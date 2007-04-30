import re, threading, os.path, inspect, sys, cStringIO, itertools
import cgi, cgitb, urllib, Cookie, mimetypes

from operator import itemgetter

from pony.thirdparty.cherrypy.wsgiserver import CherryPyWSGIServer

from pony import auth
from pony.utils import decorator_with_params
from pony.templating import Html
from pony.logging import log, log_exc

re_component = re.compile("""
        [$]
        (?: (\d+)              # param number (group 1)
        |   ([A-Za-z_]\w*)     # param identifier (group 2)
        )$
    |   (                      # path component (group 3)
            (?:[$][$] | [^$])*
        )$                     # end of string
    """, re.VERBOSE)

@decorator_with_params
def http(url=None, redirect=False, **params):
    params = dict([ (name.replace('_', '-').title(), value)
                    for name, value in params.items() ])
    def new_decorator(old_func):
        real_url = url is None and old_func.__name__ or url
        register_http_handler(old_func, real_url, redirect, params)
        return old_func
    return new_decorator

def register_http_handler(func, url, redirect, params):
    return HttpInfo(func, url, redirect, params)

http_registry_lock = threading.Lock()
http_registry = ({}, [])

def split_url(url, strict_parsing=False):
    if isinstance(url, unicode): url = url.encode('utf8')
    elif isinstance(url, str):
        if strict_parsing:
            try: url.decode('ascii')
            except UnicodeDecodeError: raise ValueError(
                'Url string contains non-ascii symbols. '
                'Such urls must be in unicode.')
    else: raise ValueError('Url parameter must be str or unicode')
    if '?' in url:
        p, q = url.split('?', 1)
        qlist = []
        qnames = set()
        for name, value in cgi.parse_qsl(q, strict_parsing=strict_parsing,
                                            keep_blank_values=True):
            if name not in qnames:
                qlist.append((name, value))
                qnames.add(name)
            elif strict_parsing:
                raise ValueError('Duplicate url parameter: %s' % name)
    else: p, qlist = url, []
    p, ext = os.path.splitext(p)
    components = p.split('/')
    if not components[0]: components = components[1:]
    path = map(urllib.unquote, components)
    return path, ext, qlist

class HttpInfo(object):
    def __init__(self, func, url, redirect, params):
        self.func = func
        if not hasattr(func, 'argspec'):
            func.argspec = self.getargspec(func)
            func.dummy_func = self.create_dummy_func(func)
        self.url = url
        self.path, self.ext, self.qlist = split_url(url, strict_parsing=True)
        self.redirect = redirect
        self.params = params
        self.args = set()
        self.keyargs = set()
        self.parsed_path = map(self.parse_component, self.path)
        self.parsed_query = []
        for name, value in self.qlist:
            is_param, x = self.parse_component(value)
            self.parsed_query.append((name, is_param, x))
        self.check()
        self.register()
    @staticmethod
    def getargspec(func):
        original_func = getattr(func, 'original_func', func)
        names,argsname,keyargsname,defaults = inspect.getargspec(original_func)
        names = list(names)
        if defaults is None: new_defaults = []
        else: new_defaults = list(defaults)
        try:
            for i, value in enumerate(new_defaults):
                if value is not None:
                    new_defaults[i] = unicode(value).encode('utf8')
        except UnicodeDecodeError:
            raise ValueError('Default value contains non-ascii symbols. '
                             'Such default values must be in unicode.')
        return names, argsname, keyargsname, new_defaults
    @staticmethod
    def create_dummy_func(func):
        spec = inspect.formatargspec(*func.argspec)[1:-1]
        source = "lambda %s: __locals__()" % spec
        return eval(source, dict(__locals__=locals))
    def parse_component(self, component):
        match = re_component.match(component)
        if not match: raise ValueError('Invalid url component: %r' % component)
        i = match.lastindex
        if i == 1: return True, self.adjust(int(match.group(i)) - 1)
        elif i == 2: return True, self.adjust(match.group(i))
        elif i == 3: return False, match.group(i).replace('$$', '$')
        else: assert False
    def adjust(self, x):
        names, argsname, keyargsname, defaults = self.func.argspec
        args, keyargs = self.args, self.keyargs
        if isinstance(x, int):
            if x < 0 or x >= len(names) and argsname is None:
                raise TypeError('Invalid parameter index: %d' % (x+1))
            if x in args:
                raise TypeError('Parameter index %d already in use' % (x+1))
            args.add(x)
            return x
        elif isinstance(x, basestring):
            try: i = names.index(x)
            except ValueError:
                if keyargsname is None or x in keyargs:
                    raise TypeError('Invalid parameter name: %s' % x)
                keyargs.add(x)
                return x
            else:
                if i in args: raise TypeError(
                    'Parameter name %s already in use' % x)
                args.add(i)
                return i
        assert False
    def check(self):
        names, argsname, keyargsname, defaults = self.func.argspec
        args, keyargs = self.args, self.keyargs
        for i, name in enumerate(names[:len(names)-len(defaults)]):
            if i not in args:
                raise TypeError('Undefined path parameter: %s' % name)
        if args:
            for i in range(len(names), max(args)):
                if i not in args:
                    raise TypeError('Undefined path parameter: %d' % (i+1))
    def register(self):
        d1 = {}
        for i, (is_param, x) in enumerate(self.parsed_path): d1[i] = is_param
        for name, is_param, x in self.parsed_query: d1[name] = is_param
        qdict = dict(self.qlist)
        http_registry_lock.acquire()
        try:
            for info,_,_ in get_http_handlers(self.path, self.ext, qdict):
                d2 = {}
                for i, (is_param, x) in enumerate(info.parsed_path):
                    d2[i] = is_param
                for name, is_param, x in info.parsed_query:
                    d2[name] = is_param
                if d1 == d2: _http_remove(info)
            d, list = http_registry
            for is_param, x in self.parsed_path:
                if is_param: d, list = d.setdefault(None, ({}, []))
                else: d, list = d.setdefault(x, ({}, []))
            self.list = list
            self.func.__dict__.setdefault('http', []).insert(0, self)
            list.insert(0, self)
        finally: http_registry_lock.release()
            
class PathError(Exception): pass

def url(func, *args, **keyargs):
    http_list = getattr(func, 'http')
    if http_list is None:
        raise ValueError('Cannot create url for this object :%s' % func)
    first, second = [], []
    for info in http_list:
        if not info.redirect: first.append(info)
        else: second.append(info)
    for info in first + second:
        try:
            url = build_url(info, func, args, keyargs)
        except PathError: pass
        else: break
    else:
        raise PathError('Suitable url path for %s() not found' % func.__name__)
    return url
make_url = url

def build_url(info, func, args, keyargs):
    try: keyparams = func.dummy_func(*args, **keyargs).copy()
    except TypeError, e:
        raise TypeError(e.args[0].replace('<lambda>', func.__name__))
    names, argsname, keyargsname, defaults = func.argspec
    indexparams = map(keyparams.pop, names)
    indexparams.extend(keyparams.pop(argsname, ()))
    keyparams.update(keyparams.pop(keyargsname, {}))
    try:
        for i, value in enumerate(indexparams):
            if value is not None: indexparams[i] = unicode(value).encode('utf8')
        for key, value in keyparams.items():
            if value is not None: keyparams[key] = unicode(value).encode('utf8')
    except UnicodeDecodeError:
        raise ValueError('Url parameter value contains non-ascii symbols. '
                         'Such values must be in unicode.')
    path = []
    used_indexparams = set()
    used_keyparams = set()
    offset = len(names) - len(defaults)

    def build_param(x):
        if isinstance(x, int):
            value = indexparams[x]
            used_indexparams.add(x)
            is_default = (offset <= x < len(names)
                          and defaults[x - offset] == value)
            return is_default, value
        elif isinstance(x, basestring):
            try: value = keyparams[x]
            except KeyError: assert False, 'Parameter not found: %s' % x
            used_keyparams.add(x)
            return False, value
        else: assert False

    for is_param, x in info.parsed_path:
        if not is_param: component = x
        else:
            is_default, component = build_param(x)
            if component is None:
                raise PathError('Value for parameter %s is None' % x)
        path.append(urllib.quote(component, safe=':@&=+$,'))
    p = '/'.join(path)

    qlist = []
    for name, is_param, x in info.parsed_query:
        if not is_param: qlist.append((name, x))
        else:
            is_default, value = build_param(x)
            if not is_default:
                if value is None:
                    raise PathError('Value for parameter %s is None' % x)
                qlist.append((name, value))
    quote_plus = urllib.quote_plus
    q = "&".join(("%s=%s" % (quote_plus(name), quote_plus(value)))
                 for name, value in qlist)

    errmsg = 'Not all parameters were used during path construction'
    if len(used_keyparams) != len(keyparams):
        raise PathError(errmsg)
    if len(used_indexparams) != len(indexparams):
        for i, value in enumerate(indexparams):
            if (i not in used_indexparams
                and value != defaults[i-offset]):
                    raise PathError(errmsg)

    if not q: return '/%s%s' % (p, info.ext)
    else: return '/%s%s?%s' % (p, info.ext, q)

link_template = Html(u'<a href="%s">%s</a>')

def link(*args, **keyargs):
    description = None
    if isinstance(args[0], basestring):
        description = args[0]
        func = args[1]
        args = args[2:]
    else:
        func = args[0]
        args = args[1:]
        if func.__doc__ is None: description = func.__name__
        else: description = Html(func.__doc__.split('\n', 1)[0])
    href = url(func, *args, **keyargs)
    return link_template % (href, description)

if not mimetypes.inited: # Copied from SimpleHTTPServer
    mimetypes.init() # try to read system mime.types
extensions_map = mimetypes.types_map.copy()
extensions_map.update({
    '': 'application/octet-stream', # Default
    '.py': 'text/plain',
    '.c': 'text/plain',
    '.h': 'text/plain',
    })

def guess_type(ext):
    result = extensions_map.get(ext)
    if result is not None: return result
    result = extensions_map.get(ext.lower())
    if result is not None: return result
    return 'application/octet-stream'

def get_static_dir_name():
    main = sys.modules['__main__']
    try: script_name = main.__file__
    except AttributeError:  # interactive mode
        return None
    head, tail = os.path.split(script_name)
    return os.path.join(head, 'static')

static_dir = get_static_dir_name()

path_re = re.compile(r"^[-_.!~*'()A-Za-z0-9]+$")

def get_static_file(path, ext):
    for component in path:
        if not path_re.match(component): return None
    if ext and not path_re.match(ext): return None
    fname = os.path.join(static_dir, *path) + ext
    if not os.path.isfile(fname): return None
    headers = local.response.headers
    headers['Content-Type'] = guess_type(ext)
    headers['Expires'] = '0'
    headers['Cache-Control'] = 'max-age=10'
    return file(fname, 'rb')

def get_http_handlers(path, ext, qdict):
    # http_registry_lock.acquire()
    # try:
    variants = [ http_registry ]
    for i, component in enumerate(path):
        new_variants = []
        for d, list in variants:
            variant = d.get(component)
            if variant: new_variants.append(variant)
            if component:
                variant = d.get(None)
                if variant: new_variants.append(variant)
        variants = new_variants
    # finally: http_registry_lock.release()

    result = []
    not_found = object()
    for _, list in variants:
        for info in list:
            if ext != info.ext: continue
            args, keyargs = {}, {}
            const_count = 0
            for i, (is_param, x) in enumerate(info.parsed_path):
                if not is_param:
                    const_count += 1
                    continue
                value = path[i]
                if isinstance(x, int): args[x] = value
                elif isinstance(x, basestring): keyargs[x] = value
                else: assert False
            names, _, _, defaults = info.func.argspec
            offset = len(names) - len(defaults)
            non_used_query_params = set(qdict)
            for name, is_param, x in info.parsed_query:
                non_used_query_params.discard(name)
                value = qdict.get(name, not_found)
                if not is_param:
                    if value != x: break
                    const_count += 1
                elif isinstance(x, int):
                    if value is not_found:
                        if offset <= x < len(names): continue
                        else: break
                    else: args[x] = value
                elif isinstance(x, basestring):
                    if value is not_found: break
                    keyargs[x] = value
                else: assert False
            else:
                arglist = [ None ] * len(names)
                arglist[-len(defaults):] = defaults
                for i, value in sorted(args.items()):
                    try: arglist[i] = value
                    except IndexError:
                        assert i == len(arglist)
                        arglist.append(value)
                result.append((info, arglist, keyargs, const_count,
                               len(non_used_query_params)))
    if result:
        x = max(map(itemgetter(3), result))
        result = [ tup for tup in result if tup[3] == x ]
        x = min(map(itemgetter(4), result))
        result = [ tup[:3] for tup in result if tup[4] == x ]
    return result

def invoke(url):
    path, ext, qlist = split_url(url)
    qdict = dict(qlist)
    local.response = HttpResponse()
    handlers = get_http_handlers(path, ext, qdict)
    if not handlers:
        file = get_static_file(path, ext)
        if file is not None: return file
        if '?' in url:
            p, q = url.split('?', 1)
            if p.endswith('/'): p = p[:-1]
            else: p += '/'
            url = '?'.join((p, q))
        elif url.endswith('/'): url = url[:-1]
        else: url += '/'
        path, ext, qlist = split_url(url)
        qdict = dict(qlist)
        if get_http_handlers(path, ext, qdict):
            if not url.startswith('/'): url = '/' + url
            raise HttpRedirect(url)
        raise Http404('Page not found')
    info, args, keyargs = handlers[0]
    for i, value in enumerate(args):
        if value is not None: args[i] = value.decode('utf8')
    for key, value in keyargs.items():
        if value is not None: keyargs[key] = value.decode('utf8')
    if info.redirect:
        for alternative in info.func.http:
            if not alternative.redirect:
                new_url = make_url(info.func, *args, **keyargs)
                status = '301 Moved Permanently'
                if isinstance(info.redirect, basestring): status = info.redirect
                elif isinstance(info.redirect, (int, long)) \
                     and 300 <= info.redirect < 400: status = str(info.redirect)
                raise HttpRedirect(new_url, status)
    local.response.headers.update(info.params)
    result = info.func(*args, **keyargs)

    headers = dict([ (name.replace('_', '-').title(), value)
                     for name, value in local.response.headers.items() ])
    local.response.headers = headers
    type = headers.pop('Type', 'text/plain')
    charset = headers.pop('Charset', 'UTF-8')
    content_type = headers.get('Content-Type')
    if content_type:
        content_type_params = cgi.parse_header(content_type)[1]
        charset = content_type_params.get('charset', 'iso-8859-1')
    else: headers['Content-Type'] = '%s; charset=%s' % (type, charset)
    if isinstance(result, Html):
        headers['Content-Type'] = 'text/html; charset=%s' % charset

    if isinstance(result, unicode): result = result.encode(charset)
    elif not isinstance(result, str):
        try: result = str(result)
        except UnicodeEncodeError:
            result = unicode(result, charset, 'replace')
    headers.setdefault('Expires', '0')
    max_age = headers.pop('Max-Age', '2')
    cache_control = headers.get('Cache-Control')
    if not cache_control: headers['Cache-Control'] = 'max-age=%s' % max_age
    headers.setdefault('Vary', 'Cookie')
    return result
http.invoke = invoke

def _http_remove(info):
    info.list.remove(info)
    info.func.http.remove(info)
            
def http_remove(x):
    if isinstance(x, basestring):
        path, ext, qlist = split_url(x, strict_parsing=True)
        qdict = dict(qlist)
        http_registry_lock.acquire()
        try:
            for info, _, _ in get_http_handlers(path, ext, qdict):
                _http_remove(info)
        finally: http_registry_lock.release()
    elif hasattr(x, 'http'):
        http_registry_lock.acquire()
        try: _http_remove(x)
        finally: http_registry_lock.release()
    else: raise ValueError('This object is not bound to url: %r' % x)

http.remove = http_remove

def _http_clear(dict, list):
    for info in list: info.func.http.remove(info)
    list[:] = []
    for dict2, list2 in dict.itervalues(): _http_clear(dict2, list2)
    dict.clear()

def http_clear():
    http_registry_lock.acquire()
    try: _http_clear(*http_registry)
    finally: http_registry_lock.release()

http.clear = http_clear

################################################################################

class HttpException(Exception):
    content = ''

class Http404(HttpException):
    status = '404 Not Found'
    headers = {'Content-Type': 'text/plain'}
    def __init__(self, content='Page not found'):
        Exception.__init__(self, 'Page not found')
        self.content = content

class HttpRedirect(HttpException):
    status_dict = {'301' : '301 Moved Permanently',
                   '302' : '302 Found',
                   '303' : '303 See Other',
                   '305' : '305 Use Proxy',
                   '307' : '307 Temporary Redirect'}
    def __init__(self, location, status='302 Found'):
        Exception.__init__(self, location)
        self.location = location
        status = str(status)
        self.status = self.status_dict.get(status, status)
        self.headers = {'Location': location}

################################################################################

class HttpRequest(object):
    def __init__(self, environ):
        self.environ = environ
        self.method = environ.get('REQUEST_METHOD', 'GET')
        self.cookies = Cookie.SimpleCookie()
        if 'HTTP_COOKIE' in environ:
            self.cookies.load(environ['HTTP_COOKIE'])
        morsel = self.cookies.get('pony')
        session_data = morsel and morsel.value or None
        auth.load(session_data, environ)
        input_stream = environ.get('wsgi.input') or cStringIO.StringIO()
        self.fields = cgi.FieldStorage(
            fp=input_stream, environ=environ, keep_blank_values=True)
        self.submitted_form = self.fields.getfirst('_f')
        self.ticket_is_valid = auth.verify_ticket(self.fields.getfirst('_t'))
        self.id_counter = itertools.imap('id_%d'.__mod__, itertools.count())

class HttpResponse(object):
    def __init__(self):
        self.headers = {}
        self.cookies = Cookie.SimpleCookie()
        self._http_only_cookies = set()

class Local(threading.local):
    def __init__(self):
        self.request = HttpRequest({})
        self.response = HttpResponse()

local = Local()        

def get_request():
    return local.request

def get_response():
    return local.response

def get_param(name, default=None):
    return local.request.fields.getfirst(name, default)

def get_cookie(name, default=None):
    morsel = local.request.cookies.get(name)
    if morsel is None: return default
    return morsel.value

def set_cookie(name, value, expires=None, max_age=None, path=None, domain=None,
               secure=False, http_only=False, comment=None, version=None):
    response = local.response
    cookies = response.cookies
    if value is None:
        cookies.pop(name, None)
        response._http_only_cookies.discard(name)
    else:
        cookies[name] = value
        morsel = cookies[name]
        if expires is not None: morsel['expires'] = expires
        if max_age is not None: morsel['max-age'] = max_age
        if path is not None: morsel['path'] = path
        if domain is not None: morsel['domain'] = domain
        if comment is not None: morsel['comment'] = comment
        if version is not None: morsel['version'] = version
        if secure: morsel['secure'] = True
        if http_only: response._http_only_cookies.add(name)
        else: response._http_only_cookies.discard(name)

def format_exc():
    exc_type, exc_value, traceback = sys.exc_info()
    if traceback.tb_next: traceback = traceback.tb_next
    if traceback.tb_next: traceback = traceback.tb_next
    try:
        io = cStringIO.StringIO()
        hook = cgitb.Hook(file=io)
        hook.handle((exc_type, exc_value, traceback))
        return io.getvalue()
    finally:
        del traceback

def reconstruct_url(environ):
    url = environ['wsgi.url_scheme']+'://'
    if environ.get('HTTP_HOST'): url += environ['HTTP_HOST']
    else:
        url += environ['SERVER_NAME']
        if environ['wsgi.url_scheme'] == 'https':
            if environ['SERVER_PORT'] != '443':
                url += ':' + environ['SERVER_PORT']
        elif environ['SERVER_PORT'] != '80':
            url += ':' + environ['SERVER_PORT']

    url += urllib.quote(environ.get('SCRIPT_NAME',''))
    url += urllib.quote(environ.get('PATH_INFO',''))
    if environ.get('QUERY_STRING'):
        url += '?' + environ['QUERY_STRING']
    return url

def log_request(environ):
    headers=dict((key, value) for key, value in environ.items()
                              if isinstance(key, basestring)
                              and isinstance(value, basestring))
    log(type='HTTP:%s' % environ.get('REQUEST_METHOD', 'GET'),
        text=reconstruct_url(environ),
        headers=headers)

http_only_incompatible_browsers = [ 'WebTV', 'MSIE 5.0; Mac' ]

ONE_MONTH = 60*60*24*31

def create_cookies(environ):
    data, domain, path = auth.save(environ)
    if data is not None:
        set_cookie('pony', data, ONE_MONTH, ONE_MONTH, path or '/', domain,
                   http_only=True)
    user_agent = environ.get('HTTP_USER_AGENT', '')
    support_http_only = True
    for browser in http_only_incompatible_browsers:
        if browser in user_agent:
            support_http_only = False
            break
    response = local.response
    result = []
    for name, morsel in response.cookies.items():
        cookie = morsel.OutputString()
        if support_http_only and name in response._http_only_cookies:
            cookie += ' HttpOnly'
        result.append(('Set-Cookie', cookie))
    return result

BLOCK_SIZE = 65536

def wsgi_app(environ, wsgi_start_response):
    def start_response(status, headers):
        headers = [ (name, str(value)) for name, value in headers.items() ]
        headers.extend(create_cookies(environ))
        log(type='HTTP:response', text=status, headers=headers)
        wsgi_start_response(status, headers)

    local.request = HttpRequest(environ)
    url = environ['PATH_INFO']
    query = environ['QUERY_STRING']
    if query: url = '%s?%s' % (url, query)
    try:
        log_request(environ)
        result = invoke(url)
    except HttpException, e:
        start_response(e.status, e.headers)
        return [ e.content ]
    except:
        log_exc()
        start_response('500 Internal Server Error',
                       {'Content-Type': 'text/html'})
        return [ format_exc() ]
    else:
        response = local.response
        start_response('200 OK', response.headers)
        if hasattr(result, 'read'): # result is file
            # return [ result.read() ]
            return iter(lambda: result.read(BLOCK_SIZE), '')
        return [ result ]

def wsgi_test(environ, start_response):
    stdout = cStringIO.StringIO()
    h = environ.items(); h.sort()
    for k,v in h:
        print >>stdout, k,'=',`v`
    start_response('200 OK', [ ('Content-Type', 'text/plain') ])
    return [ stdout.getvalue() ]

wsgi_apps = [('', wsgi_app), ('/pony/', wsgi_test)]

def parse_address(address):
    if isinstance(address, basestring):
        if ':' in address:
            host, port = address.split(':')
            return host, int(port)
        else:
            return address, 80
    assert len(address) == 2
    return tuple(address)

server_threads = {}

class ServerStartException(Exception): pass
class ServerStopException(Exception): pass

class ServerThread(threading.Thread):
    def __init__(self, host, port, wsgi_app, verbose):
        server = server_threads.setdefault((host, port), self)
        if server != self: raise ServerStartException(
            'HTTP server already started: %s:%s' % (host, port))
        threading.Thread.__init__(self)
        self.host = host
        self.port = port
        self.server = CherryPyWSGIServer(
            (host, port), wsgi_apps, server_name=host)
        self.verbose = verbose
        self.setDaemon(False)
    def run(self):
        msg = 'Starting HTTP server at %s:%s' % (self.host, self.port)
        log('HTTP:start', msg)
        if self.verbose: print msg
        self.server.start()
        msg = 'HTTP server at %s:%s stopped successfully' \
              % (self.host, self.port)
        log('HTTP:start', msg)
        if self.verbose: print msg
        server_threads.pop((self.host, self.port), None)

def start_http_server(address='localhost:8080', verbose=True):
    host, port = parse_address(address)
    server_thread = ServerThread(host, port, wsgi_app, verbose=verbose)
    server_thread.start()

def stop_http_server(address=None):
    if address is None:
        for server_thread in server_threads.values():
            server_thread.server.stop()
    else:
        host, port = parse_address(address)
        server_thread = server_threads.get((host, port))
        if server_thread is None: raise ServerStopException(
            'Cannot stop HTTP server at %s:%s '
            'because it is not started:' % (host, port))
        server_thread.server.stop()
    