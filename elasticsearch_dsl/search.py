from .query import Q, EMPTY_QUERY, FilteredQuery
from .filter import F, EMPTY_FILTER
from .aggs import A, AggBase
from .utils import DslBase
from .result import Response


class BaseProxy(object):
    def __init__(self, search, attr_name):
        self._search = search
        self._proxied = self._empty
        self._attr_name = attr_name

    def __nonzero__(self):
        return self._proxied != self._empty
    __bool__ = __nonzero__

    def __call__(self, *args, **kwargs):
        s = self._search._clone()
        getattr(s, self._attr_name)._proxied += self._shortcut(*args, **kwargs)

        # always return search to be chainable
        return s

    def __getattr__(self, attr_name):
        return getattr(self._proxied, attr_name)

    def __setattr__(self, attr_name, value):
        if not attr_name.startswith('_'):
            self._proxied = self._shortcut(self._proxied.to_dict())
            setattr(self._proxied, attr_name, value)
        super(BaseProxy, self).__setattr__(attr_name, value)


class ProxyQuery(BaseProxy):
    _empty = EMPTY_QUERY
    _shortcut = staticmethod(Q)


class ProxyFilter(BaseProxy):
    _empty = EMPTY_FILTER
    _shortcut = staticmethod(F)


class AggsProxy(AggBase, DslBase):
    name = 'aggs'
    def __init__(self, search):
        self._base = self._search = search
        self._params = {'aggs': {}}

    def to_dict(self):
        return super(AggsProxy, self).to_dict().get('aggs', {})


class Search(object):
    def __init__(self, using=None, index=None, doc_type=None, extra=None):
        """
        Search request to elasticsearch.

        :arg using: `Elasticsearch` instance to use
        :arg index: limit the search to index
        :arg doc_type: only query this type.

        All the paramters supplied (or omitted) at creation type can be later
        overriden by methods (`using`, `index` and `doc_type` respectively).
        """
        self._using = using

        self._index = None
        if isinstance(index, (tuple, list)):
            self._index = list(index)
        elif index:
            self._index = [index]

        self._doc_type = None
        if isinstance(doc_type, (tuple, list)):
            self._doc_type = list(doc_type)
        elif doc_type:
            self._doc_type = [doc_type]

        self.query = ProxyQuery(self, 'query')
        self.filter = ProxyFilter(self, 'filter')
        self.post_filter = ProxyFilter(self, 'post_filter')
        self.aggs = AggsProxy(self)
        self._sort = []
        self._extra = extra or {}
        self._params = {}
        self._fields = []

    def __getitem__(self, n):
        """
        Support slicing the `Search` instance for pagination.

        Slicing equates to the from/size parameters. E.g.::

            s = Search().query(...)[0:25]

        is equivalent to::

            s = Search().query(...).extra(from_=0, size=25)

        """
        s = self._clone()

        if isinstance(n, slice):
            # If negative slicing, abort.
            if n.start and n.start < 0 or n.stop and n.stop < 0:
                raise ValueError("Search does not support negative slicing.")
            # Elasticsearch won't get all results so we default to size: 10 if
            # stop not given.
            s._extra['from'] = n.start or 0
            s._extra['size'] = n.stop - (n.start or 0) if n.stop else 10
            return s
        else:  # This is an index lookup, equivalent to slicing by [n:n+1].
            # If negative index, abort.
            if n < 0:
                raise ValueError("Search does not support negative indexing.")
            s._extra['from'] = n
            s._extra['size'] = 1
            return s

    @classmethod
    def from_dict(cls, d):
        """
        Construct a `Search` instance from a raw dict containing the search
        body.
        """
        s = cls()
        s.update_from_dict(d)
        return s

    def _clone(self):
        """
        Return a clone of the current search request. Performs a shallow copy
        of all the underlying objects. Used internally by most state modifying APIs.
        """
        s = Search(using=self._using, index=self._index, doc_type=self._doc_type)
        s._sort = self._sort[:]
        s._fields = self._fields[:]
        s._extra = self._extra.copy()
        for x in ('query', 'filter', 'post_filter'):
            getattr(s, x)._proxied = getattr(self, x)._proxied

        # copy top-level bucket definitions
        if self.aggs._params.get('aggs'):
            s.aggs._params = {'aggs': self.aggs._params['aggs'].copy()}
        s._params = self._params.copy()
        return s

    def update_from_dict(self, d):
        """
        Apply options from a serialized body to the current instance. Modifies
        the object in-place.
        """
        d = d.copy()
        self.query._proxied = Q(d.pop('query'))
        if 'post_filter' in d:
            self.post_filter._proxied = F(d.pop('post_filter'))

        if isinstance(self.query._proxied, FilteredQuery):
            self.filter._proxied = self.query._proxied.filter
            self.query._proxied = self.query._proxied.query

        aggs = d.pop('aggs', d.pop('aggregations', {}))
        if aggs:
            self.aggs._params = {
                'aggs': dict(
                    (name, A({name: value})) for (name, value) in aggs.items())
            }
        if 'sort' in d:
            self._sort = d.pop('sort')
        if 'fields' in d:
            self._fields = d.pop('fields')
        self._extra = d

    def params(self, **kwargs):
        """
        Specify query params to be used when executing the search. All the
        keyword arguments will override the current values.
        """
        s = self._clone()
        s._params.update(kwargs)
        return s

    def extra(self, **kwargs):
        """
        Add extra keys to the request body.
        """
        s = self._clone()
        if 'from_' in kwargs:
            kwargs['from'] = kwargs.pop('from_')
        s._extra.update(kwargs)
        return s

    def fields(self, *args):
        """
        Selectively load specific stored fields for each document.
        """
        s = self._clone()
        s._fields = list(args)
        return s

    def sort(self, *keys):
        """
        Add sorting information to the search request. If called without
        arguments it will remove all sort requirements. Otherwise it will
        replace them. Acceptable arguments are::

            'some.field'
            '-some.other.fiels'
            {'different.field': {'any': 'dict'}}

        so for example::

            s = Search().sort(
                'category',
                '-title',
                {"price" : {"order" : "asc", "mode" : "avg"}}
            )

        will sort by ``category``, ``title`` (in descending order) and
        ``price`` in ascending order using the ``avg`` mode.

        The API returns a copy of the Search object and can thus be chained.
        """
        s = self._clone()
        s._sort = []
        for k in keys:
            if isinstance(k, str) and k.startswith('-'):
                k = {k[1:]: {"order": "desc"}}
            s._sort.append(k)
        return s

    def index(self, *index):
        """
        Set the index for the search. If called empty it will rmove all information.

        Example:

            s = Search().index('twitter')
        """
        # .index() resets
        s = self._clone()
        if not index:
            s._index = None
        else:
            s._index = (self._index or []) + list(index)
        return s

    def doc_type(self, *doc_type):
        """
        Set the type to search through. You can supply a single value or a
        list. If no index is supplied (or an empty value) any information
        stored on the instance will be erased.
        """
        # .doc_type() resets
        s = self._clone()
        if not doc_type:
            s._doc_type = None
        else:
            s._doc_type = (self._doc_type or []) + list(doc_type)
        return s

    def to_dict(self, count=False, **kwargs):
        """
        Serialize the search into the dictionary that will be sent over as the
        request's body.

        :arg count: a flag to specify we are interested in a body for count -
            no aggregations, no pagination bounds etc.

        All additional keyword arguments will be included into the dictionary.
        """
        if self.filter:
            d = {
              "query": {
                "filtered": {
                  "query": self.query.to_dict(),
                  "filter": self.filter.to_dict()
                }
              }
            }
        else:
            d = {"query": self.query.to_dict()}

        if self.post_filter:
            d['post_filter'] = self.post_filter.to_dict()

        # count request doesn't care for sorting and other things
        if not count:
            if self.aggs.aggs:
                d.update(self.aggs.to_dict())

            if self._sort:
                d['sort'] = self._sort

            d.update(self._extra)

            if self._fields:
                d['fields'] = self._fields

        d.update(kwargs)
        return d

    def using(self, client):
        """
        Associate the search request with an elasticsearch client. A fresh copy
        will be returned with current instance remaining unchanged.

        :arg client: and instance of ``elasticsearch.Elasticsearch`` to use
        """
        s = self._clone()
        s._using = client
        return s

    def count(self):
        """
        Return the number of hits matching the query and filters. Note that
        only the actual number is returned.
        """
        if not self._using:
            raise #XXX

        d = self.to_dict(count=True)
        # TODO: failed shards detection
        return self._using.count(
            index=self._index,
            doc_type=self._doc_type,
            body=d
        )['count']

    def execute(self):
        """
        Execute the search and return an instance of ``Response`` wrapping all
        the data.
        """
        if not self._using:
            raise #XXX

        return Response(
            self._using.search(
                index=self._index,
                doc_type=self._doc_type,
                body=self.to_dict(),
                **self._params
            )
        )

