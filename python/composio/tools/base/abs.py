"""Base abstractions."""

import hashlib
import inspect
import json
import typing as t
from abc import abstractmethod
from pathlib import Path

import inflection
import jsonref
import pydantic
from pydantic import BaseModel, Field

from composio.client.enums import Action as ActionEnum
from composio.exceptions import ComposioSDKError
from composio.utils.logging import WithLogger


GroupID = t.Literal["runtime", "local", "api"]
ModelType = t.TypeVar("ModelType")
ActionResponse = t.TypeVar("ActionResponse")
ActionRequest = t.TypeVar("ActionRequest")
Loadable = t.TypeVar("Loadable")
ToolRegistry = t.Dict[GroupID, t.Dict[str, "Tool"]]
ActionsRegistry = t.Dict[GroupID, t.Dict[str, "Action"]]
TriggersRegistry = t.Dict[GroupID, t.Dict[str, t.Any]]

tool_registry: ToolRegistry = {"runtime": {}, "local": {}, "api": {}}
action_registry: ActionsRegistry = {"runtime": {}, "local": {}, "api": {}}
trigger_registry: TriggersRegistry = {"runtime": {}, "local": {}, "api": {}}


def remove_json_ref(data: t.Dict) -> t.Dict:
    return json.loads(
        jsonref.dumps(
            jsonref.replace_refs(
                obj=data,
                lazy_load=False,
            ),
            indent=2,
        )
    )


def generate_app_id(name: str) -> str:
    # Generate a 32-character hash using MD5
    hash_string = hashlib.md5(name.encode()).hexdigest()
    # Insert hyphens at the specified positions
    return "-".join(
        (
            hash_string[:8],
            hash_string[8:12],
            hash_string[12:16],
            hash_string[16:20],
            hash_string[20:],
        )
    )


class InvalidClassDefinition(ComposioSDKError):
    """Raise when a class is not defined properly."""


class ExecuteResponse(BaseModel):
    """Execute action response."""


class _Attributes:
    name: str
    """Name represenation."""

    enum: str
    """Enum key."""

    display_name: str
    """Display compatible name."""

    description: str
    """Description string."""


class _Request(t.Generic[ModelType]):
    """Request util."""

    def __init__(self, model: t.Type[ModelType]) -> None:
        """Initialize request model."""
        self.model = model

    def schema(self) -> t.Dict:
        """Build request schema."""
        request = t.cast(t.Type[BaseModel], self.model).model_json_schema(by_alias=True)
        request = remove_json_ref(request)
        if "$defs" in request:
            del request["$defs"]

        properties = request.get("properties", {})
        for prop in properties.values():
            if prop.get("file_readable", False):
                prop["oneOf"] = [
                    {
                        "type": prop.get("type"),
                        "description": prop.get("description", ""),
                    },
                    {
                        "type": "string",
                        "format": "file-path",
                        "description": f"File path to {prop.get('description', '')}",
                    },
                ]
                del prop["type"]  # Remove original type to avoid conflict in oneOf
                continue

            if (
                "allOf" in prop
                and len(prop["allOf"]) == 1
                and "enum" in prop["allOf"][0]
            ):
                (schema,) = prop.pop("allOf")
                prop.update(schema)
                prop[
                    "description"
                ] += f" Note: choose value only from following options - {prop['enum']}"

        request["properties"] = properties
        return request

    def parse(self, request: t.Dict) -> ModelType:
        """Parse request."""
        try:
            return self.model(**request)
        except pydantic.ValidationError as e:
            message = "Invalid request data provided"
            missing = []
            others = [""]
            for error in e.errors():
                param = ".".join(map(str, error["loc"]))
                if error["type"] == "missing":
                    missing.append(param)
                    continue
                others.append(error["msg"] + f" on parameter `{param}`")
            if len(missing) > 0:
                message += f"\n- Following fields are missing: {set(missing)}"
            message += "\n- ".join(others)
            raise ValueError(message) from e


class _Response(t.Generic[ModelType]):
    """Response util."""

    def __init__(self, model: t.Type[ModelType]) -> None:
        """Initialize request model."""
        self.model = model
        self.wrapper = self.wrap(model=model)

    @classmethod
    def wrap(cls, model: t.Type[ModelType]) -> t.Type[BaseModel]:
        class wrapper(model):  # type: ignore
            successful: bool = Field(
                ...,
                description="Whether or not the action execution was successful or not",
            )
            error: t.Optional[str] = Field(
                None,
                description="Error if any occured during the execution of the action",
            )

        return t.cast(t.Type[BaseModel], wrapper)

    def schema(self) -> t.Dict:
        """Build request schema."""
        schema = self.wrapper.model_json_schema(by_alias=True)
        schema["title"] = self.model.__name__
        return remove_json_ref(schema)


class ActionBuilder:
    @staticmethod
    def set_generics(name: str, obj: t.Type["Action"]) -> None:
        try:
            (generic,) = getattr(obj, "__orig_bases__")
            request, response = t.get_args(generic)
            if request == ActionRequest or response == ActionResponse:
                raise ValueError(f"Invalid type generics, ({request}, {response})")
        except ValueError as e:
            raise InvalidClassDefinition(
                "Invalid action class definition, please define your class "
                "using request and response type generics; "
                f"class {name}(Action[RequestModel, ResponseModel])"
            ) from e

        setattr(obj, "request", _Request(request))
        setattr(obj, "response", _Response(response))

    @staticmethod
    def validate(name: str, obj: t.Type["Action"]) -> None:
        if getattr(getattr(obj, "execute"), "__isabstractmethod__", False):
            raise InvalidClassDefinition(f"Please implement {name}.execute")

    @staticmethod
    def set_metadata(obj: t.Type["Action"]) -> None:
        setattr(obj, "file", getattr(obj, "file", Path(inspect.getfile(obj))))
        setattr(obj, "name", getattr(obj, "name", inflection.underscore(obj.__name__)))
        setattr(
            obj,
            "enum",
            getattr(obj, "enum", inflection.underscore(obj.__name__).upper()),
        )
        setattr(
            obj,
            "display_name",
            getattr(
                obj,
                "display_name",
                inflection.humanize(inflection.underscore(obj.__name__)),
            ),
        )
        setattr(
            obj,
            "description",
            (obj.__doc__ or obj.display_name).lstrip().rstrip(),
        )


class ActionMeta(type):
    """Action metaclass."""

    def __init__(  # pylint: disable=unused-argument, self-cls-assignment
        cls,
        name: str,
        bases: t.Tuple,
        dict_: t.Dict,
        abs: bool = False,
    ) -> None:
        """Initialize action class."""
        if abs or name == "Action":
            return

        cls = t.cast(t.Type["Action"], cls)
        ActionBuilder.validate(name=name, obj=cls)
        ActionBuilder.set_generics(name=name, obj=cls)
        ActionBuilder.set_metadata(obj=cls)


class Action(
    WithLogger,
    _Attributes,
    t.Generic[ActionRequest, ActionResponse],
    metaclass=ActionMeta,
):
    """Action abstraction."""

    _tags: t.Optional[t.List[str]] = None

    _schema: t.Optional[t.Dict] = None

    tool: str
    """Toolname."""

    request: _Request[ActionRequest]
    """Request helper."""

    response: _Response[ActionResponse]
    """Response helper."""

    file: str
    """Path to the file containing the action"""

    requires: t.Optional[t.List[str]] = None
    """List of dependencies required to run this action."""

    no_auth: bool = False
    """If set `True`, the action does not require a connected account."""

    def __init_subclass__(cls, abs: bool = False) -> None:
        """Initialize subclas."""

    @classmethod
    def tags(cls) -> t.List[str]:
        """Tags for the given action."""
        return cls._tags or []

    @classmethod
    def _generate_schema(cls) -> None:
        """Generate action schema."""
        description = (
            cls.__doc__.lstrip().rstrip()
            if cls.__doc__
            else inflection.titleize(cls.display_name)
        )
        cls._schema = {
            "name": cls.name,
            "enum": cls.enum,
            "appName": cls.tool,
            "appId": generate_app_id(cls.tool),
            "tags": cls.tags(),
            "displayName": cls.display_name,
            "description": description,
            "parameters": cls.request.schema(),
            "response": cls.response.schema(),
        }

    @classmethod
    def schema(cls) -> t.Dict:
        """Action schema."""
        if cls._schema is None:
            cls._generate_schema()
        return cls._schema  # type: ignore

    @abstractmethod
    def execute(
        self,
        request: ActionRequest,
        metadata: t.Dict,
    ) -> ActionResponse:
        """Execute the action."""


class ToolBuilder:
    @staticmethod
    def validate(obj: t.Type["Tool"], name: str, methods: t.Tuple[str, ...]) -> None:
        for method in methods:
            if getattr(getattr(obj, method), "__isabstractmethod__", False):
                raise InvalidClassDefinition(f"Please implement {name}.{method}")

            if not inspect.ismethod(getattr(obj, method)):
                raise InvalidClassDefinition(
                    f"Please implement {name}.{method} as class method"
                )

    @staticmethod
    def set_metadata(obj: t.Type["Tool"]) -> None:
        setattr(obj, "file", Path(inspect.getfile(obj)))
        setattr(obj, "gid", getattr(obj, "gid", "local"))
        setattr(obj, "name", getattr(obj, "name", inflection.underscore(obj.__name__)))
        setattr(obj, "enum", getattr(obj, "enum", obj.name).upper())
        setattr(
            obj,
            "display_name",
            getattr(
                obj,
                "display_name",
                inflection.humanize(inflection.underscore(obj.__name__)),
            ),
        )
        setattr(obj, "description", (obj.__doc__ or obj.display_name).lstrip().rstrip())
        setattr(obj, "_actions", getattr(obj, "_actions", {}))
        setattr(obj, "_triggers", getattr(obj, "_triggers", {}))

    @staticmethod
    def setup_children(obj: t.Type["Tool"]) -> None:
        if obj.gid not in action_registry:
            action_registry[obj.gid] = {}

        for action in obj.actions():
            action.tool = obj.name
            action.enum = f"{obj.enum}_{action.name.upper()}"
            obj._actions[action.enum] = action  # pylint: disable=protected-access
            action_registry[obj.gid][action.enum] = action  # type: ignore

        if not hasattr(obj, "triggers"):
            return

        if obj.gid not in trigger_registry:
            trigger_registry[obj.gid] = {}

        for trigger in obj.triggers():  # type: ignore
            trigger.tool = obj.name
            trigger.enum = f"{obj.enum}_{trigger.name.upper()}"
            obj._triggers[trigger.enum] = trigger  # type: ignore  # pylint: disable=protected-access
            trigger_registry[obj.gid][trigger.enum] = trigger  # type: ignore


class Tool(WithLogger, _Attributes):
    """Tool abstraction."""

    gid: GroupID
    """Group ID for this tool."""

    file: Path
    """Path to module file."""

    name: str
    """Tool name."""

    _schema: t.Optional[t.Dict] = None
    """Schema for the app."""

    _actions: t.Dict[str, t.Type[Action]]
    """Actions container"""

    def __init_subclass__(cls, autoload: bool = False) -> None:
        """Initialize a tool class."""

    @classmethod
    def get(cls, enum: ActionEnum) -> t.Type[Action]:
        """Returns the"""
        return cls._actions[enum.slug]

    @classmethod
    @abstractmethod
    def actions(cls) -> t.List[t.Type[t.Any]]:
        """Get collection of actions for the tool."""

    @classmethod
    def _generate_schema(cls) -> None:
        """Generate schema for the app."""
        cls._schema = {
            "name": cls.name,
            "displayName": cls.display_name,
            "metaData": {
                "toolName": cls.name,
                "groupId": cls.gid,
                "displayName": cls.display_name,
                "description": cls.description,
                "toolPath": str(cls.file),
            },
            "integration": {},
            "description": cls.description,
            "actions": [action.schema() for action in cls.actions()],
        }

    @classmethod
    def schema(cls) -> t.Dict:
        """Get tool schema."""
        if cls._schema is None:
            cls._generate_schema()
        return cls._schema  # type: ignore

    def _load(self, loadable: t.Type[Loadable]) -> Loadable:
        """Load action class."""
        instance = loadable()
        return instance

    def execute(
        self,
        action: str,
        params: t.Dict,
        metadata: t.Optional[t.Dict] = None,
    ) -> t.Dict:
        """
        Execute the given action

        :param action: Name of the action.
        :param params: Execution parameters.
        :param metadata: A dictionary containing metadata for action.
        """
        raise NotImplementedError()

    @classmethod
    def register(cls: t.Type["Tool"]) -> None:
        """Register given tool to the registry."""
        # TODO(Viraj): Check if the tool is already registered
        if cls.gid not in tool_registry:
            tool_registry[cls.gid] = {}
        tool_registry[cls.gid][cls.enum] = cls()
