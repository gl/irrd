import logging
from datetime import datetime
from typing import List, Set

import sqlalchemy as sa
from IPy import IP
from sqlalchemy.dialects import postgresql as pg

from irrd.utils.validators import parse_as_number, ValidationError
from . import engine
from .models import RPSLDatabaseObject
from irrd.rpsl.parser import RPSLObject
from irrd.rpsl.rpsl_objects import lookup_field_names

logger = logging.getLogger(__name__)
MAX_RECORDS_CACHE_BEFORE_INSERT = 5000


class RPSLDatabaseQuery:
    """
    RPSL data query builder for retrieving RPSL objects.

    Offers various ways to filter, which are always constructed in an AND query.
    For example:
        q = RPSLDatabaseQuery().sources(['NTTCOM']).asn(23456)
    would match all objects that refer or include AS23456 (i.e. aut-num, route,
    as-block, route6) from the NTTCOM source.

    For methods taking a prefix or IP address, this should be an IPy.IP object.
    """
    table = RPSLDatabaseObject.__table__
    columns = RPSLDatabaseObject.__table__.c
    lookup_field_names = lookup_field_names()

    def __init__(self):
        self.statement = sa.select([
            self.columns.pk,
            self.columns.object_class,
            self.columns.rpsl_pk,
            self.columns.parsed_data,
            self.columns.object_text,
            self.columns.source,
        ]).order_by(self.columns.ip_first.asc(), self.columns.asn_first.asc())
        self._lookup_attr_counter = 0
        self._query_frozen = False

    def pk(self, pk: str):
        """Filter on an exact object PK (UUID)."""
        return self._filter(self.columns.pk == pk)

    def rpsl_pk(self, rpsl_pk: str):
        """Filter on an exact RPSL PK (e.g. 192.0.2.0/24,AS23456)."""
        return self._filter(self.columns.rpsl_pk == rpsl_pk.upper())

    def sources(self, sources: List[str]):
        """
        Filter on one or more sources.

        Sources list must be an iterable. Will match objects from any
        of the mentioned sources.
        """
        fltr = self.columns.source.in_([s.upper() for s in sources])
        return self._filter(fltr)

    def object_classes(self, object_classes: List[str]):
        """
        Filter on one or more object classes.

        Classes list must be an iterable. Will match objects from any
        of the mentioned classes.
        """
        fltr = self.columns.object_class.in_(object_classes)
        return self._filter(fltr)

    def lookup_attr(self, attr_name: str, attr_value: str):
        """Filter on a lookup attribute, e.g. mnt-by."""
        attr_name = attr_name.lower()
        if attr_name not in self.lookup_field_names:
            raise ValueError(f"Invalid lookup attribute: {attr_name}")
        self._check_query_frozen()

        counter = ++self._lookup_attr_counter
        fltr = sa.text(f"parsed_data->:lookup_attr_name{counter} ? :lookup_attr_value{counter}")
        self.statement = self.statement.where(fltr).params(
            **{f"lookup_attr_name{counter}": attr_name,
               f"lookup_attr_value{counter}": attr_value
               }
        )
        return self

    def ip_exact(self, ip: IP):
        """
        Filter on an exact prefix or address.

        The provided ip should be an IPy.IP class, and can be a prefix or
        an address.
        """
        fltr = sa.and_(
            self.columns.ip_first == str(ip.net()),
            self.columns.ip_last == str(ip.broadcast()),
            self.columns.ip_version == ip.version()
        )
        return self._filter(fltr)

    def ip_less_specific(self, ip: IP):
        """Filter any less specifics or exact matches of a prefix."""
        fltr = sa.and_(
            self.columns.ip_first <= str(ip.net()),
            self.columns.ip_last >= str(ip.broadcast()),
            self.columns.ip_version == ip.version()
        )
        return self._filter(fltr)

    def ip_less_specific_one_level(self, ip: IP):
        """
        Filter one level less specific of a prefix.

        Due to implementation details around filtering, this must
        always be the last call on a query object, or unpredictable
        results may occur.
        """
        self._check_query_frozen()
        # One level less specific could still have multiple objects.
        # A subquery determines the smallest possible size less specific object,
        # and this is then used to filter for any objects with that size.
        fltr = sa.and_(
            self.columns.ip_first <= str(ip.net()),
            self.columns.ip_last >= str(ip.broadcast()),
            self.columns.ip_version == ip.version(),
            sa.not_(sa.and_(self.columns.ip_first == str(ip.net()), self.columns.ip_last == str(ip.broadcast()))),
        )
        self.statement = self.statement.where(fltr)

        size_subquery = self.statement.with_only_columns([self.columns.ip_size])
        size_subquery = size_subquery.order_by(self.columns.ip_size.asc())
        size_subquery = size_subquery.limit(1)

        self.statement = self.statement.where(self.columns.ip_size.in_(size_subquery))
        self._query_frozen = True
        return self

    def ip_more_specific(self, ip: IP):
        """Filter any more specifics of a prefix.

        Note that this only finds full more specifics: objects for which their
        IP range is fully encompassed by the ip parameter.
        """
        fltr = sa.and_(
            self.columns.ip_first >= str(ip.net()),
            self.columns.ip_last <= str(ip.broadcast()),
            self.columns.ip_version == ip.version(),
            sa.not_(sa.and_(self.columns.ip_first == str(ip.net()), self.columns.ip_last == str(ip.broadcast()))),
        )
        return self._filter(fltr)

    def asn(self, asn: int):
        """
        Filter for a specific ASN.

        This will match all objects that refer to this ASN, or a block
        encompassing it - including route, route6, aut-num and as-block.
        For more exact matches, add a filter on object class.
        """
        fltr = sa.and_(self.columns.asn_first <= asn, self.columns.asn_last >= asn)
        return self._filter(fltr)

    def text_search(self, value: str):
        """
        Search the database for a specific free text.

        In order, this attempts:
        - If the value is a valid AS number, return all as-block, as-set, aut-num objects
          relating or including that AS number.
        - If the value is a valid IP address or network, return all objects that relate to
          that resource and any less specifics.
        - Otherwise, return all objects where the RPSL primary key is exactly this value,
          or (case insensitive) it matches part of a person/role name (not nic-hdl, their
          actual person/role attribute value).
        """
        self._check_query_frozen()
        try:
            _, asn = parse_as_number(value)
            return self.object_classes(['as-block', 'as-set', 'aut-num']).asn(asn)
        except ValidationError:
            pass

        try:
            ip = IP(value)
            return self.ip_less_specific(ip)
        except ValueError:
            pass

        counter = ++self._lookup_attr_counter
        fltr = sa.or_(
            self.columns.rpsl_pk == value,
            sa.and_(
                self.columns.object_class == 'person',
                sa.text(f"parsed_data->>'person' ILIKE :lookup_attr_text_search{counter}")
            ),
            sa.and_(
                self.columns.object_class == 'role',
                sa.text(f"parsed_data->>'role' ILIKE :lookup_attr_text_search{counter}")
            ),
        )
        self.statement = self.statement.where(fltr).params(
            **{f'lookup_attr_text_search{counter}': '%' + value + '%'}
        )
        return self

    def first_only(self):
        """Only return the first match."""
        self.statement = self.statement.limit(1)
        return self

    def _filter(self, fltr):
        self._check_query_frozen()
        self.statement = self.statement.where(fltr)
        return self

    def _check_query_frozen(self):
        if self._query_frozen:
            raise ValueError("This query was frozen - no more filters can be applied.")

    def __repr__(self):
        return f"RPSLDatabaseQuery: {self.statement}\nPARAMS: {self.statement.compile().params}"


class DatabaseHandler:
    """
    Interface for other parts of IRRD to talk to the database.

    Note that no writes to the database are final until commit()
    has been called - and rollback() can be called at any time
    to all submitted changes.
    """
    def __init__(self):
        self._rpsl_upsert_cache: List[dict] = []
        self._rpsl_pk_source_seen: Set[str] = set()
        self._connection = engine.connect()
        self._start_transaction()

    def execute_query(self, query: RPSLDatabaseQuery):
        """Execute an RPSLDatabaseQuery within the current transaction."""
        # To be able to query objects that were just created, flush the cache.
        self._flush_rpsl_object_upsert_cache()
        logger.debug(f'Executing: {query}')
        result = self._connection.execute(query.statement)
        for row in result:
            yield dict(row)
        result.close()

    def upsert_rpsl_object(self, rpsl_object: RPSLObject):
        """
        Schedule an RPSLObject for insertion/updating.

        This method will insert the object, or overwrite an existing object
        if it has the same RPSL primary key and source. No other checks are
        applied before overwriting.

        Writes may not be issued to the database immediately for performance
        reasons, but commit() will ensure all writes are flushed to the DB first.
        """
        ip_first = str(rpsl_object.ip_first) if rpsl_object.ip_first else None
        ip_last = str(rpsl_object.ip_last) if rpsl_object.ip_last else None

        ip_size = None
        if rpsl_object.ip_first and rpsl_object.ip_last:
            ip_size = rpsl_object.ip_last.int() - rpsl_object.ip_first.int() + 1

        # In some cases, multiple updates may be submitted for the same object.
        # PostgreSQL will not allow rows proposed for insertion to have duplicate
        # contstrained values - so if a second object appears with a pk/source
        # seen before, the cache must be flushed right away, or the two updates
        # will conflict.
        rpsl_pk_source = rpsl_object.pk() + "-" + rpsl_object.parsed_data['source']
        if rpsl_pk_source in self._rpsl_pk_source_seen:
            self._flush_rpsl_object_upsert_cache()

        self._rpsl_upsert_cache.append({
            'rpsl_pk': rpsl_object.pk(),
            'source': rpsl_object.parsed_data['source'],
            'object_class': rpsl_object.rpsl_object_class,
            'parsed_data': rpsl_object.parsed_data,
            'object_text': rpsl_object.render_rpsl_text(),
            'ip_version': rpsl_object.ip_version(),
            'ip_first': ip_first,
            'ip_last': ip_last,
            'ip_size': ip_size,
            'asn_first': rpsl_object.asn_first,
            'asn_last': rpsl_object.asn_last,
            'updated': datetime.utcnow(),
        })
        self._rpsl_pk_source_seen.add(rpsl_pk_source)

        if len(self._rpsl_upsert_cache) > MAX_RECORDS_CACHE_BEFORE_INSERT:
            self._flush_rpsl_object_upsert_cache()

    def commit(self):
        """
        Commit any pending changes to the database and start a fresh transaction.
        """
        self._flush_rpsl_object_upsert_cache()
        try:
            self.transaction.commit()
            self._start_transaction()
        except Exception:  # pragma: no cover - TODO: log the exception and details and report back an error state
            self.transaction.rollback()
            raise

    def rollback(self):
        """Roll back the current transaction, discarding all submitted changes."""
        self._rpsl_upsert_cache = []
        self._rpsl_pk_source_seen = set()
        self.transaction.rollback()

    def _flush_rpsl_object_upsert_cache(self):
        """
        Flush the current upsert cache to the database.

        This happens in one large INSERT .. ON CONFLICT DO UPDATE ..
        statement, which is more performant than individual queries
        in case of large datasets.
        """
        if not self._rpsl_upsert_cache:
            return

        rpsl_composite_key = ['rpsl_pk', 'source']
        stmt = pg.insert(RPSLDatabaseObject).values(self._rpsl_upsert_cache)
        columns_to_update = {
            c.name: c
            for c in stmt.excluded
            if c.name not in rpsl_composite_key and c.name != 'pk'
        }

        update_stmt = stmt.on_conflict_do_update(
            index_elements=rpsl_composite_key,
            set_=columns_to_update,
        )

        try:
            self._connection.execute(update_stmt)
        except Exception:  # pragma: no cover - TODO: log the exception and details and report back an error state
            self.transaction.rollback()
            raise

        self._rpsl_upsert_cache = []
        self._rpsl_pk_source_seen = set()

    def _start_transaction(self):
        """Start a fresh transaction."""
        self.transaction = self._connection.begin()
