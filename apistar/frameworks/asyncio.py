import json
import typing

from apistar import Settings, commands, exceptions, http
from apistar.components import (
    commandline, console, dependency, router, schema, sessions, statics,
    templates, umi
)
from apistar.core import Command, Component
from apistar.frameworks.cli import CliApp
from apistar.interfaces import (
    Auth, CommandLineClient, Console, FileWrapper, Injector, Router, Schema,
    SessionStore, StaticFiles, Templates
)
from apistar.types import (
    Handler, KeywordArgs, ReturnValue, UMIChannels, UMIMessage
)


class ASyncIOApp(CliApp):
    INJECTOR_CLS = dependency.AsyncDependencyInjector

    BUILTIN_COMMANDS = [
        Command('new', commands.new),
        Command('run', commands.run_asyncio),
        Command('schema', commands.schema),
        Command('test', commands.test)
    ]

    BUILTIN_COMPONENTS = [
        Component(Schema, init=schema.CoreAPISchema),
        Component(Templates, init=templates.Jinja2Templates),
        Component(StaticFiles, init=statics.WhiteNoiseStaticFiles),
        Component(Router, init=router.WerkzeugRouter),
        Component(CommandLineClient, init=commandline.ArgParseCommandLineClient),
        Component(Console, init=console.PrintConsole),
        Component(SessionStore, init=sessions.LocalMemorySessionStore),
    ]

    HTTP_COMPONENTS = [
        Component(http.Method, init=umi.get_method),
        Component(http.URL, init=umi.get_url),
        Component(http.Scheme, init=umi.get_scheme),
        Component(http.Host, init=umi.get_host),
        Component(http.Port, init=umi.get_port),
        Component(http.Path, init=umi.get_path),
        Component(http.Headers, init=umi.get_headers),
        Component(http.Header, init=umi.get_header),
        Component(http.QueryString, init=umi.get_querystring),
        Component(http.QueryParams, init=umi.get_queryparams),
        Component(http.QueryParam, init=umi.get_queryparam),
        Component(http.Body, init=umi.get_body),
        Component(http.Request, init=http.Request),
        Component(http.RequestStream, init=umi.get_stream),
        Component(http.RequestData, init=umi.get_request_data),
        Component(FileWrapper, init=umi.get_file_wrapper),
        Component(http.Session, init=sessions.get_session),
        Component(Auth, init=umi.get_auth)
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        # Setup everything that we need in order to run `self.__call__()`
        self.router = self.preloaded_state[Router]
        self.http_injector = self.create_http_injector()

    def create_http_injector(self) -> Injector:
        """
        Create the dependency injector for running handlers in response to
        incoming HTTP requests.
        """
        http_components = {
            component.cls: component.init
            for component in self.HTTP_COMPONENTS
        }

        return self.INJECTOR_CLS(
            components={**http_components, **self.components},
            initial_state=self.preloaded_state,
            required_state={
                UMIMessage: 'message',
                UMIChannels: 'channels',
                KeywordArgs: 'kwargs',
                Handler: 'handler',
                Exception: 'exc',
                http.ResponseHeaders: 'response_headers'
            },
            resolvers=[dependency.HTTPResolver()]
        )

    async def __call__(self,
                       message: typing.Dict[str, typing.Any],
                       channels: typing.Dict[str, typing.Any]):
        headers = http.ResponseHeaders()
        state = {
            'message': message,
            'channels': channels,
            'handler': None,
            'kwargs': None,
            'exc': None,
            'response_headers': headers
        }
        method = message['method'].upper()
        path = message['path']
        try:
            handler, kwargs = self.router.lookup(path, method)
            state['handler'], state['kwargs'] = handler, kwargs
            funcs = [self.check_permissions, handler, self.finalize_response]
            response = await self.http_injector.run_all_async(funcs, state=state)
        except Exception as exc:
            state['exc'] = exc  # type: ignore
            funcs = [self.exception_handler, self.finalize_response]
            response = await self.http_injector.run_all_async(funcs, state=state)

        headers.update(response.headers)
        if response.content_type is not None:
            headers['content-type'] = response.content_type

        response_message = {
            'status': response.status,
            'headers': [
                [key.encode(), value.encode()]
                for key, value in headers
            ],
            'content': response.content
        }
        await channels['reply'].send(response_message)

    def exception_handler(self, exc: Exception) -> http.Response:
        if isinstance(exc, exceptions.Found):
            return http.Response('', exc.status_code, {'Location': exc.location})

        if isinstance(exc, exceptions.HTTPException):
            if isinstance(exc.detail, str):
                content = {'message': exc.detail}
            else:
                content = exc.detail
            return http.Response(content, exc.status_code, {})

        raise

    async def check_permissions(self, handler: Handler, injector: Injector, settings: Settings):
        default_permissions = settings.get('PERMISSIONS', None)
        permissions = getattr(handler, 'permissions', default_permissions)
        if permissions is None:
            return

        for permission in permissions:
            if not await injector.run_async(permission.has_permission):
                raise exceptions.Forbidden()

    def finalize_response(self, ret: ReturnValue) -> http.Response:
        if isinstance(ret, http.Response):
            data, status, headers, content_type = ret
            if content_type is not None:
                return ret
        else:
            data, status, headers, content_type = ret, 200, {}, None

        if data is None:
            content = b''
            content_type = None
        elif isinstance(data, str):
            content = data.encode('utf-8')
            content_type = 'text/html; charset=utf-8'
        elif isinstance(data, bytes):
            content = data
            content_type = 'text/html; charset=utf-8'
        else:
            content = json.dumps(data).encode('utf-8')
            content_type = 'application/json'

        if not content and status == 200:
            status = 204
            content_type = None

        return http.Response(content, status, headers, content_type)
