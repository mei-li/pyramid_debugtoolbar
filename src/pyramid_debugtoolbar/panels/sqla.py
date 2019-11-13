import threading
import time
import weakref

from pyramid.httpexceptions import HTTPBadRequest
from pyramid.threadlocal import get_current_request
from pyramid.view import view_config

from pyramid_debugtoolbar.compat import json
from pyramid_debugtoolbar.compat import url_quote
from pyramid_debugtoolbar.panels import DebugPanel
from pyramid_debugtoolbar.utils import format_sql
from pyramid_debugtoolbar.utils import text_
from pyramid_debugtoolbar.utils import STATIC_PATH
from pyramid_debugtoolbar.utils import ROOT_ROUTE_NAME

lock = threading.Lock()

try:
    from sqlalchemy import event
    from sqlalchemy.engine.base import Engine

    @event.listens_for(Engine, "before_cursor_execute")
    def _before_cursor_execute(conn, cursor, stmt, params, context, execmany):
        setattr(conn, 'pdtb_start_timer', time.time())

    @event.listens_for(Engine, "after_cursor_execute")
    def _after_cursor_execute(conn, cursor, stmt, params, context, execmany):
        stop_timer = time.time()
        request = get_current_request()
        if request is not None and hasattr(request, 'pdtb_sqla_queries'):
            with lock:
                engines = request.registry.pdtb_sqla_engines
                engines[id(conn.engine)] = weakref.ref(conn.engine)
                queries = request.pdtb_sqla_queries
                duration = (stop_timer - conn.pdtb_start_timer) * 1000
                queries.append({
                    'engine_id': id(conn.engine),
                    'duration': duration,
                    'statement': stmt,
                    'parameters': params,
                    'context': context
                })
        delattr(conn, 'pdtb_start_timer')

    has_sqla = True
except ImportError:
    has_sqla = False

_ = lambda x: x


class SQLADebugPanel(DebugPanel):
    """
    Panel that displays the SQL generated by SQLAlchemy plus the time each
    SQL statement took in milliseconds.
    """
    name = 'sqlalchemy'
    template = 'pyramid_debugtoolbar.panels:templates/sqlalchemy.dbtmako'
    title = _('SQLAlchemy Queries')
    nav_title = _('SQLAlchemy')

    def __init__(self, request):
        self.queries = request.pdtb_sqla_queries = []
        if hasattr(request.registry, 'pdtb_sqla_engines'):
            self.engines = request.registry.pdtb_sqla_engines
        else:
            self.engines = request.registry.pdtb_sqla_engines = {}
        self.pdtb_id = request.pdtb_id

    @property
    def has_content(self):
        if self.queries:
            return True
        else:
            return False

    @property
    def nav_subtitle(self):
        if self.queries:
            return "%d" % (len(self.queries))

    def process_response(self, response):
        data = []
        for index, query in enumerate(self.queries):
            stmt = query['statement']

            is_select = stmt.strip().lower().startswith('select')
            params = ''
            try:
                params = url_quote(json.dumps(query['parameters']))
            except TypeError:
                pass  # object not JSON serializable
            except ValueError:
                pass  # JSON cyclic can errors generate ValueError exceptions
            except UnicodeDecodeError:
                pass  # parameters contain non-utf8 (probably binary) data

            data.append({
                'engine_id': query['engine_id'],
                'duration': query['duration'],
                'sql': format_sql(stmt),
                'raw_sql': stmt,
                'parameters': query['parameters'],
                'params': params,
                'is_select': is_select,
                'context': query['context'],
                'query_index': index,
            })

        self.data = {
            'queries': data,
            'text': text_,
            'engines': self.engines,
        }

    def render_content(self, request):
        if not self.queries:
            return 'No queries in executed in request.'
        return super(SQLADebugPanel, self).render_content(request)

    def render_vars(self, request):
        return {
            'pdtb_id': self.pdtb_id,
            'route_url': request.route_url,
            'static_path': request.static_url(STATIC_PATH),
            'root_path': request.route_url(ROOT_ROUTE_NAME)
        }


class SQLAlchemyViews(object):
    def __init__(self, request):
        self.request = request

    def find_query(self):
        request_id = self.request.matchdict['request_id']
        toolbar = self.request.pdtb_history.get(request_id)
        if toolbar is None:
            raise HTTPBadRequest('No history found for request.')
        sqlapanel = [p for p in toolbar.panels if p.name == 'sqlalchemy'][0]
        query_index = int(self.request.matchdict['query_index'])
        return sqlapanel.queries[query_index]

    @view_config(
        route_name='debugtoolbar.sql_select',
        renderer=(
            'pyramid_debugtoolbar.panels:templates/sqlalchemy_select.dbtmako'
        ),
    )
    def sql_select(self):
        query_dict = self.find_query()
        stmt = query_dict['statement']
        engine_id = query_dict['engine_id']
        params = query_dict['parameters']
        # Make sure it is a select statement
        if not stmt.lower().strip().startswith('select'):
            raise HTTPBadRequest('Not a SELECT SQL statement')

        if not engine_id:
            raise HTTPBadRequest('No valid database engine')

        engines = self.request.registry.parent_registry.pdtb_sqla_engines
        engine = engines[int(engine_id)]()
        result = engine.execute(stmt, params)

        return {
            'result': result.fetchall(),
            'headers': result.keys(),
            'sql': format_sql(stmt),
            'duration': float(query_dict['duration']),
        }

    @view_config(
        route_name='debugtoolbar.sql_explain',
        renderer=(
            'pyramid_debugtoolbar.panels:templates/sqlalchemy_explain.dbtmako'
        ),
    )
    def sql_explain(self):
        query_dict = self.find_query()
        stmt = query_dict['statement']
        engine_id = query_dict['engine_id']
        params = query_dict['parameters']

        if not engine_id:
            raise HTTPBadRequest('No valid database engine')

        engines = self.request.registry.parent_registry.pdtb_sqla_engines
        engine = engines[int(engine_id)]()

        if engine.name.startswith('sqlite'):
            query = 'EXPLAIN QUERY PLAN %s' % stmt
        else:
            query = 'EXPLAIN %s' % stmt

        result = engine.execute(query, params)

        return {
            'result': result.fetchall(),
            'headers': result.keys(),
            'sql': format_sql(stmt),
            'str': str,
            'duration': float(query_dict['duration']),
        }


def includeme(config):
    config.add_route(
        'debugtoolbar.sql_select',
        '/{request_id}/sqlalchemy/select/{query_index}')
    config.add_route(
        'debugtoolbar.sql_explain',
        '/{request_id}/sqlalchemy/explain/{query_index}')

    config.add_debugtoolbar_panel(SQLADebugPanel)
    config.scan(__name__)
