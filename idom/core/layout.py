import abc
import asyncio
from types import coroutine
from typing import (
    List,
    Dict,
    Tuple,
    Mapping,
    NamedTuple,
    Any,
    Set,
    Generic,
    TypeVar,
    Optional,
    Iterator,
    AsyncIterator,
    Awaitable,
    TypeVar,
    Union,
)

from .element import AbstractElement
from .events import EventHandler
from .utils import HasAsyncResources, async_resource
from .hooks import LifeCycleHook


_Self = TypeVar("_Self")


class LayoutUpdate(NamedTuple):
    """An object describing an update to a :class:`Layout`"""

    src: str
    """element ID for the update's source"""

    new: Dict[str, Dict[str, Any]]
    """maps element IDs to new models"""

    old: List[str]
    """element IDs that have been deleted"""

    errors: List[Exception]
    """A list of errors that occured while rendering"""


class LayoutEvent(NamedTuple):
    target: str
    """The ID of the event handler."""
    data: List[Any]
    """A list of event data passed to the event handler."""


class AbstractLayout(HasAsyncResources, abc.ABC):
    """Renders the models generated by :class:`AbstractElement` objects.

    Parameters:
        root: The root element of the layout.
        loop: What loop the layout should be using to schedule tasks.
    """

    __slots__ = ["_loop", "_root"]

    if not hasattr(abc.ABC, "__weakref__"):  # pragma: no cover
        __slots__.append("__weakref__")

    def __init__(
        self, root: "AbstractElement", loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> None:
        super().__init__()
        if loop is None:
            loop = asyncio.get_event_loop()
        if not isinstance(root, AbstractElement):
            raise TypeError("Expected an AbstractElement, not %r" % root)
        self._loop = loop
        self._root = root

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """The event loop the layout is using."""
        return self._loop

    @property
    def root(self) -> str:
        """Id of the root element."""
        return self._root.id

    @abc.abstractmethod
    async def render(self) -> LayoutUpdate:
        """Await an update to the model."""

    @abc.abstractmethod
    def update(self, element: AbstractElement) -> None:
        """Schedule the element to be re-renderer."""

    @abc.abstractmethod
    async def trigger(self, event: LayoutEvent) -> None:
        """Trigger an event handler

        Parameters:
            event: Event data passed to the event handler.
        """

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._root})"


class ElementState(NamedTuple):
    model: Dict[str, Any]
    element_obj: AbstractElement
    event_handler_ids: Set[str]
    child_elements_ids: List[str]
    life_cycle_hook: LifeCycleHook


class Layout(AbstractLayout):

    __slots__ = "_event_handlers"

    def __init__(
        self, root: "AbstractElement", loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> None:
        super().__init__(root, loop)
        self._event_handlers: Dict[str, EventHandler] = {}

    def update(self, element: "AbstractElement") -> None:
        self._rendering_queue.put(self._render_element(element))

    async def trigger(self, event: LayoutEvent) -> None:
        # It is possible for an element in the frontend to produce an event
        # associated with a backend model that has been deleted. We only handle
        # events if the element and the handler exist in the backend. Otherwise
        # we just ignore the event.
        handler = self._event_handlers.get(event.target)
        if handler is not None:
            await handler(event.data)

    async def render(self) -> Dict[str, Any]:
        await self._rendering_queue.get()
        return self._element_states[self._root.id].model

    @async_resource
    async def _rendering_queue(self) -> AsyncIterator["FutureQueue[LayoutUpdate]"]:
        queue: FutureQueue[LayoutUpdate] = FutureQueue()
        queue.put(self._render_element(self._root))
        try:
            yield queue
        finally:
            await queue.cancel()

    @async_resource
    async def _element_states(self) -> AsyncIterator[ElementState]:
        root_element_state = self._create_element_state(self._root)
        try:
            yield {self._root.id: root_element_state}
        finally:
            self._unmount_element_state(root_element_state)

    async def _render_element(self, element: AbstractElement) -> Dict[str, Any]:
        if element.id not in self._element_states:
            self._element_states[element.id] = self._create_element_state(element)

        element_state = self._element_states[element.id]

        element_state.life_cycle_hook.element_will_render()

        self._clear_element_state_event_handlers(element_state)
        self._unmount_element_state_children(element_state)

        # BUG: https://github.com/python/mypy/issues/9256
        raw_model = await _render_with_life_cycle_hook(element_state)  # type: ignore

        if isinstance(raw_model, AbstractElement):
            raw_model = {"tagName": "div", "children": [raw_model]}

        resolved_model = await self._render_model(element_state, raw_model)
        element_state.model.clear()
        element_state.model.update(resolved_model)

        element_state.life_cycle_hook.element_did_render()

        # We need to return the model from the `element_state` so that the model
        # between all `ElementState` objects within a `Layout` are shared.
        return element_state.model

    def _create_element_state(self, element: AbstractElement) -> ElementState:
        return ElementState(
            model={},
            element_obj=element,
            event_handler_ids=set(),
            child_elements_ids=[],
            life_cycle_hook=LifeCycleHook(element, self.update),
        )

    async def _render_model(
        self, element_state: ElementState, model: Mapping[str, Any]
    ) -> Dict[str, Any]:
        model: Dict[str, Any] = dict(model)

        model["eventHandlers"] = self._render_model_event_handlers(element_state, model)

        if "children" in model:
            model["children"] = await self._render_model_children(
                element_state, model["children"]
            )

        return model

    async def _render_model_children(
        self, element_state: ElementState, children: Union[List[Any], Tuple[Any, ...]]
    ) -> List[Any]:
        resolved_children: List[Any] = []
        for child in children if isinstance(children, (list, tuple)) else [children]:
            if isinstance(child, Mapping):
                resolved_children.append(await self._render_model(element_state, child))
            elif isinstance(child, AbstractElement):
                element_state.child_elements_ids.append(child.id)
                resolved_children.append(await self._render_element(child))
            else:
                resolved_children.append(str(child))
        return resolved_children

    def _render_model_event_handlers(
        self, element_state: ElementState, model: Mapping[str, Any]
    ) -> Dict[str, str]:
        handlers: Dict[str, EventHandler] = {}
        if "eventHandlers" in model:
            handlers.update(model["eventHandlers"])
        if "attributes" in model:
            attrs = model["attributes"]
            for k, v in list(attrs.items()):
                if callable(v):
                    if not isinstance(v, EventHandler):
                        h = handlers[k] = EventHandler()
                        h.add(attrs.pop(k))
                    else:
                        h = attrs.pop(k)
                        handlers[k] = h

        event_handlers_by_id = {h.id: h for h in handlers.values()}
        element_state.event_handler_ids.clear()
        element_state.event_handler_ids.update(event_handlers_by_id)
        self._event_handlers.update(event_handlers_by_id)

        return {e: h.serialize() for e, h in handlers.items()}

    def _reset_element_state(self, element_state: ElementState) -> None:
        self._clear_element_state_event_handlers(element_state)
        self._unmount_element_state_children(element_state)

    def _unmount_element_state(self, element_state: ElementState) -> None:
        element_state.life_cycle_hook.element_will_unmount()
        self._clear_element_state_event_handlers(element_state)
        self._unmount_element_state_children(element_state)
        del self._element_states[element_state.element_obj.id]

    def _clear_element_state_event_handlers(self, element_state: ElementState) -> None:
        for handler_id in element_state.event_handler_ids:
            del self._event_handlers[handler_id]
        element_state.event_handler_ids.clear()

    def _unmount_element_state_children(self, element_state: ElementState) -> None:
        for e_id in element_state.child_elements_ids:
            self._unmount_element_state(self._element_states[e_id])
        element_state.child_elements_ids.clear()


@coroutine
def _render_with_life_cycle_hook(element_state: ElementState) -> Iterator[None]:
    """Render an element which may use hooks.

    We use a coroutine here because we need to know when control is yielded
    back to the event loop since it might switch to render a different element.
    """
    gen = element_state.element_obj.render().__await__()
    while True:
        element_state.life_cycle_hook.set_current()
        try:
            yield next(gen)
        except StopIteration as error:
            return error.value
        finally:
            element_state.life_cycle_hook.unset_current()


# future queue type
_FQT = TypeVar("_FQT")


class FutureQueue(Generic[_FQT]):
    """A queue which returns the result of futures as they complete."""

    def __init__(self) -> None:
        self._loop = asyncio.get_event_loop()
        self._pending: Dict[int, asyncio.Future[_FQT]] = {}
        self._done: asyncio.Queue[asyncio.Future[_FQT]] = asyncio.Queue()

    def put(self, awaitable: Awaitable[_FQT]) -> None:
        """Put an awaitable in the queue

        The result will be returned by a call to :meth:`FutureQueue.get` only
        when the awaitable has completed.
        """

        async def wrapper() -> None:
            future = asyncio.ensure_future(awaitable)
            self._pending[id(future)] = future
            try:
                await future
            finally:
                del self._pending[id(future)]
                await self._done.put(future)
            return None

        asyncio.run_coroutine_threadsafe(wrapper(), self._loop)
        return None

    async def get(self) -> _FQT:
        """Get the result of a queued awaitable that has completed."""
        future = await self._done.get()
        return await future

    async def cancel(self) -> None:
        for f in self._pending.values():
            f.cancel()
        if self._pending:
            await asyncio.wait(
                list(self._pending.values()), return_when=asyncio.ALL_COMPLETED
            )
