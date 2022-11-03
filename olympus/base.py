import logging
from typing import Any, Callable, Iterator, Optional

from django.conf import settings
from django.utils import timezone
from elasticsearch import Elasticsearch, helpers


_collectors: list[tuple[str, str, type["OlympusCollector"]]] = []


class ElasticSearchClient(Elasticsearch):
    def __init__(self, hosts=None, **kwargs):
        if hosts is None:
            hosts = [settings.OLYMPUS_ELASTICSEARCH_URL]
        kwargs['verify_certs'] = kwargs.get('verify_certs', settings.OLYMPUS_ELASTICSEARCH_VERIFY_CERTS)

        if "timeout" not in kwargs:
            kwargs["timeout"] = 30

        super().__init__(hosts=hosts, **kwargs)


class OlympusCollector:
    index_name: Optional[str] = None
    index_date_pattern: Optional[str] = None
    chunk_size = 500
    alias_suffix = '-latest'

    @classmethod
    def __init_subclass__(cls):
        # register collectors - use metaclass if py < 3.6 support is required
        _collectors.append((cls.__module__.split('.')[0], cls.name(), cls))
        return super().__init_subclass__()

    def __init__(self, es: Optional[ElasticSearchClient] = None, timestamp: Optional[timezone.datetime] = None):
        self.logger = logging.getLogger(f'occ:{self.__module__}.{self.__class__.__name__}')
        self.es = es if es else ElasticSearchClient()
        if timestamp is None:
            self.timestamp = timezone.now()
        else:
            self.timestamp = timestamp

    def get_index_name(self) -> str:
        # set property for simple index name
        # or just override this method for more complex logic
        index_name = self.__get_raw_index_name()
        if self.index_date_pattern is None:
            return index_name

        return f'{index_name}-{self.timestamp.strftime(self.index_date_pattern)}'

    def __get_raw_index_name(self) -> str:
        return self.index_name or self.name().lower()

    @classmethod
    def name(cls) -> str:
        return f"{cls.__module__.split('.')[0]}.{cls.__name__}"

    def create_index(self):
        self.es.indices.create(index=self.get_index_name(), ignore=400)
        if self.index_date_pattern and self.alias_suffix:
            # Create unique alias for latest index
            alias = self.__get_raw_index_name() + self.alias_suffix
            self.es.indices.delete_alias(index='_all', name=alias, ignore=404)
            self.es.indices.put_alias(index=self.get_index_name(), name=alias)

    def fake_push(
        self,
        stats_cb: Optional[Callable[[bool, dict[str, Any]], None]] = None,
    ) -> tuple[int, list[dict[str, Any]]]:
        """
        this method collects data but does not actually push it to ES, log.debug it instead

        :param stats_cb: function called with (ok, item) as returned by streaming_bulk on every iteration
        :return: tuple of number of (success, fail) records
        """
        success = 0
        for item in self.__collect():
            success += 1
            if stats_cb is not None:
                stats_cb(True, item)
            self.logger.debug('would push: %s', item)
        return success, []

    def push(
        self,
        stats_cb: Optional[Callable[[bool, dict[str, Any]], None]] = None,
    ) -> tuple[int, list[dict[str, Any]]]:
        """
        :param stats_cb: function called with (ok, item) as returned by streaming_bulk on every iteration
        :return: tuple of number of (success, fail) records
        """
        self.create_index()
        # make streaming_bulk yield successful results so we can count them
        success = 0
        fails = []
        for ok, item in helpers.streaming_bulk(
            self.es, self.__collect(), chunk_size=self.chunk_size, yield_ok=True, raise_on_error=False
        ):
            delete_status = item.get("delete", {}).get('status')
            update_status = item.get("update", {}).get('update')
            if not ok and (delete_status == 404 or update_status == 404):
                # ignore "not_found" on delete and update...
                # consider it "ok" for stats purposes
                ok = True
            if stats_cb is not None:
                stats_cb(ok, item)
            if not ok:
                fails.append(item)
            else:
                success += 1
        return success, fails

    def __collect(self) -> Iterator[dict[str, Any]]:
        # add default fields to collected items
        for _i in self.collect():
            if '_type' not in _i:
                _i['_type'] = 'status'
            if '_index' not in _i:
                _i['_index'] = self.get_index_name()
            yield _i

    def collect(self) -> Iterator[dict[str, Any]]:
        raise NotImplementedError()

    def estimated_count(self) -> Optional[int]:
        """
        this should return as fast as possible an estimate of the amount of objects that will be generated by collect()
        this is only used for stats, so if there's no quick count for the collector, return None

        ENFORCED:
        this method should not iterate everything like collect() only to provide a count
        that would severely impact that collector performance (iterating twice...)
        if that is the only way, do not use it, just return None.

        :return: estimated number of objects to be generated or None if impossible to predict
        """
        return None


def collectors() -> list[tuple[str, str, type["OlympusCollector"]]]:
    return _collectors
