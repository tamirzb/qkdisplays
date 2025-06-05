import argparse
import dataclasses
import json
import os
import re
import socket
import sys
import typing

import i3ipc

from .types import Point


Direction = typing.Literal["left", "right"]


def construct_optional_dataclass(cls: type) -> type:
    """Construct a dataclass that all its fields are optional"""
    return dataclasses.make_dataclass(
        cls.__name__ + "Optional",
        [
            (
                field.name,
                typing.Optional[field.type],
                dataclasses.field(default=None),
            )
            for field in dataclasses.fields(cls)
        ],
    )


@dataclasses.dataclass
class Opts:
    allow_reorg: bool = True
    strict_y: bool = True


class OptsOptional(construct_optional_dataclass(Opts)):
    pass


class Displays:
    """
    A class to include all business logic of managing displays (aka outputs)
    """

    @dataclasses.dataclass
    class OutputData:
        name: str
        x: int
        y: int
        width: int
        height: int
        focused: bool

    _allow_reorg: bool
    _strict_y: bool
    _sorted_outputs: list[OutputData]

    def __init__(self, opts: Opts):
        self._allow_reorg = opts.allow_reorg
        self._strict_y = opts.strict_y
        self.calculate_outputs()

    @staticmethod
    def _get_outputs_data() -> list[OutputData]:
        outputs = i3ipc.Connection().get_outputs()

        result = []
        for output in outputs:
            if not output.active:
                continue

            output_data = Displays.OutputData(
                output.name,
                output.rect.x,
                output.rect.y,
                output.rect.width,
                output.rect.height,
                output.focused,
            )
            result.append(output_data)

        return result

    @staticmethod
    def _sort_outputs(outputs: list[OutputData]) -> list[OutputData] | None:
        """
        Get outputs sorted from left to right. If outputs are not contiguous
        returns None.
        """
        outputs = sorted(outputs, key=lambda i: i.x)
        for i, output in enumerate(outputs[1:]):
            prev_output = outputs[i]
            diff = output.x - (prev_output.x + prev_output.width)
            # I think scaling issues can sometimes mean that there is still a
            # gap of less than 1 pixel
            if diff > 1 or diff < -1:
                return None

        return outputs

    def _reorg_outputs(
        self, outputs: list[OutputData], reorg_x: bool = True
    ) -> list[OutputData]:
        """
        Reorganize the outputs to be contiguous.
        If reorg_x is False then only move the y position of the outputs.
        """
        x = min(i.x for i in outputs) if reorg_x else 0
        y = min(i.y for i in outputs) if self._strict_y else 0

        i3 = i3ipc.Connection()
        for output in sorted(outputs, key=lambda i: i.x):
            if not reorg_x:
                x = output.x
            if not self._strict_y:
                y = output.y
            i3.command(f"output {output.name} pos {x} {y}")
            output.x = x
            output.y = y
            x += output.width

        return outputs

    def calculate_outputs(self):
        outputs = self._get_outputs_data()
        if not outputs:
            # Edge case: no active outputs found so nothing to sort
            self._sorted_outputs = []
            return

        sorted_outputs = self._sort_outputs(outputs)

        if not sorted_outputs:
            if not self._allow_reorg:
                raise RuntimeError(
                    "Outputs are not contiguous from left to right but "
                    "reorganization of outputs is set to false"
                )
            sorted_outputs = self._reorg_outputs(outputs)
        elif self._strict_y and not all(
            i.y == sorted_outputs[0].y for i in sorted_outputs[1:]
        ):
            if not self._allow_reorg:
                raise RuntimeError(
                    "Outputs are not all in the same y position but "
                    "reorganization of outputs is set to false"
                )
            sorted_outputs = self._reorg_outputs(outputs, reorg_x=False)

        self._sorted_outputs = sorted_outputs

    def _get_focused(self) -> int:
        """Get the index of the currently focused output"""
        for i, output in enumerate(self._sorted_outputs):
            if output.focused:
                return i

        raise RuntimeError("No focused output found")

    def get_sorted_display_locations(self) -> typing.Iterator[Point]:
        for output in self._sorted_outputs:
            yield Point(output.x, output.y)

    def _swap_outputs(self, index1: int, index2: int) -> bool:
        """
        Swap between two outputs, specificed by their indexes.
        Returns whether an output was moved or not.
        """
        left_index = min(index1, index2)
        right_index = max(index1, index2)

        if left_index == right_index:
            return False

        # Save the x where the swapping starts
        x = self._sorted_outputs[left_index].x
        # Create a list for the new order of outputs, only for outputs we
        # should modify
        new_order = self._sorted_outputs[left_index : right_index + 1]
        # Swap between the outputs
        new_order[0], new_order[-1] = new_order[-1], new_order[0]

        # Enumerate outputs and set their x position according to the new
        # outputs order
        for output in new_order:
            output.x = x
            x += output.width

        # I wanted to only set the position of outputs that were moved, but it
        # seems that sway can sometimes decide to move other outputs if their
        # position was not configured explicitly beforehand. Since we don't
        # know what was explicitly configured before our execution, we instead
        # configure all outputs explicitly now.
        i3 = i3ipc.Connection()
        for output in self._sorted_outputs:
            i3.command(f"output {output.name} pos {output.x} {output.y}")

        return True

    def move(self, direction: Direction) -> bool:
        """
        Move the current display to the specified direction.
        Returns whether an output was moved or not.
        """
        focused = self._get_focused()
        other = focused + (-1 if direction == "left" else 1)
        # If we are already at the end then there is no need to move anything
        if other in (-1, len(self._sorted_outputs)):
            return False

        return self._swap_outputs(focused, other)

    def _does_output_number_exist(self, output_number: int) -> bool:
        return output_number > 0 and output_number <= len(self._sorted_outputs)

    def place(self, output_number: int) -> bool:
        """Place the current display where the given output number is"""
        if not self._does_output_number_exist(output_number):
            return False

        return self._swap_outputs(self._get_focused(), output_number - 1)

    def focus(self, output_number: int):
        """Focus on the output with the given output number"""
        if not self._does_output_number_exist(output_number):
            return

        output = self._sorted_outputs[output_number - 1].name
        i3ipc.Connection().command("focus output " + output)


class UnixServer:
    PATH = os.path.join(
        os.getenv("XDG_RUNTIME_DIR", "/tmp"), "qkdisplays.sock"
    )

    _socket: socket.socket
    _init: bool

    @staticmethod
    def is_running():
        return os.path.exists(UnixServer.PATH)

    @staticmethod
    def send(msg: str):
        """
        Connect to the server and send a message, only if the server is running
        """
        if not UnixServer.is_running():
            return

        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(UnixServer.PATH)
        try:
            client.sendall(msg.encode())
            resp = client.recv(1024)
            if resp != b"success":
                raise RuntimeError(
                    "Got error from server: " + resp.decode("utf-8")
                )
        finally:
            client.close()

    def __init__(self):
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket.bind(UnixServer.PATH)
        self._socket.listen(1)
        self._init = True

    def __del__(self):
        if not getattr(self, "_init", False):
            return
        self._socket.close()
        os.unlink(UnixServer.PATH)

    def wait_for_connection(self) -> socket.socket:
        connection, _ = self._socket.accept()
        return connection


def get_config_path() -> str | None:
    """
    Find the path to the config JSON
    """
    xdg_config_home = os.getenv("XDG_CONFIG_HOME")
    home = os.getenv("HOME")
    paths = []
    if xdg_config_home:
        paths.append(os.path.join(xdg_config_home, "qkdisplays.json"))
    if home:
        paths.append(os.path.join(home, ".config", "qkdisplays.json"))
    paths.append(os.path.join("/etc", "qkdisplays.json"))

    config_path = None
    for path in paths:
        if os.path.exists(path):
            config_path = path
            break

    return config_path


def get_config(
    config_path: str | None = get_config_path(),
    values: OptsOptional = OptsOptional(),
) -> Opts:
    """
    Figure out what are the opts for the current run based on provided values,
    then the config file, then the defaults
    """
    defaults = Opts()
    configs = [values, defaults]
    if config_path is not None:
        with open(config_path) as config_file:
            configs.insert(1, OptsOptional(**json.load(config_file)))

    result_dict = {}
    for config in configs:
        config_dict = dataclasses.asdict(config)
        for key, value in config_dict.items():
            if key not in result_dict and value is not None:
                result_dict[key] = value

    return Opts(**result_dict)


class Main:
    _opts: Opts

    def __init__(self, opts: Opts | None = None):
        self._opts = opts or get_config()

    def show(self):
        """
        Show a pop up on each display with a number. These numbers should be
        according to the order of the displays, starting from 1 and going from
        left to right. These numbers can quickly tell you the positions of the
        different displays, and can be used to identify a display using
        commands such as 'focus' or 'place'. To close the pop ups, run
        'qkdisplays close'.
        """
        # Some environments might have issues with GTK, so load it lazily
        from .gtk import GtkTools
        gtk_tools = GtkTools()
        gtk_tools.start_thread()

        try:
            displays = Displays(self._opts)
            gtk_tools.show_indicators(displays.get_sorted_display_locations())

            server = UnixServer()

            running = True
            while running:
                connection = server.wait_for_connection()
                try:
                    msg = connection.recv(1024)
                    if msg == b"notify":
                        displays.calculate_outputs()
                        gtk_tools.refresh_indicators(
                            displays.get_sorted_display_locations()
                        )
                    elif msg == b"close":
                        running = False
                    else:
                        raise RuntimeError("Bad socket message")

                    connection.sendall(b"success")
                except Exception as e:
                    connection.sendall(str(e).encode("utf-8"))
                finally:
                    connection.close()

        finally:
            gtk_tools.quit()

    def close(self):
        """Close a running 'qkdisplays show'"""
        UnixServer.send("close")

    def move(self, direction: Direction):
        """
        Move the currently focused display to the right or to the left
        """
        if Displays(self._opts).move(direction):
            UnixServer.send("notify")

    def focus(self, display_number: int):
        """
        Focus on the display specified by display_number
        """
        Displays(self._opts).focus(display_number)

    def place(self, display_number: int):
        """
        Replace the currently focused display with the one specified by
        display_number
        """
        if Displays(self._opts).place(display_number):
            UnixServer.send("notify")

    def refresh(self):
        """
        Normally a running 'qkdisplays show' will only refresh its pop ups in
        case the displays were moved through the qkdisplays tool. This command
        explicitly asks qkdisplays to refresh the pop ups, which can be useful
        if you moved the displays through another tool.
        """
        UnixServer.send("notify")


def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--allow-reorg",
        action=argparse.BooleanOptionalAction,
        help="Allow qkdisplays to move displays to be contiguous from left to "
        "right if they are not already are. If this is not set and the "
        "displays are not already contiguous then qkdisplays will fail",
    )
    parser.add_argument(
        "--strict-y",
        action=argparse.BooleanOptionalAction,
        help="All displays should have the same position on the Y axis. If "
        "they are not then if allow_reorg is set qkdisplays will move them "
        "to be so, otherwise it will fail",
    )
    parser.add_argument(
        "--config",
        type=str,
        help="Path to the config JSON file. If not supplied, will look at "
        "$XDG_RUNTIME_DIR/qkdisplays.json, then "
        "$HOME/.config/qkdisplays.json, then /etc/qkdisplays.json",
    )

    subparsers = parser.add_subparsers(
        dest="command", title="command", required=True
    )
    for method_name in dir(Main):
        if method_name.startswith("_"):
            continue

        method = getattr(Main, method_name)
        help_text = re.sub("\n *", " ", method.__doc__.strip())
        subparser = subparsers.add_parser(
            method_name, help=help_text, description=help_text
        )
        for param, param_type in typing.get_type_hints(method).items():
            kwargs = {}
            if typing.get_origin(param_type) == typing.Literal:
                kwargs["choices"] = typing.get_args(param_type)
            else:
                kwargs["type"] = param_type
            subparser.add_argument(param, **kwargs)

    return parser


def main():
    args = vars(get_parser().parse_args())

    # Figure out the opts based on args/config
    opts_dict = {k: args[k] for k in Opts.__annotations__.keys()}
    opts = get_config(args["config"], OptsOptional(**opts_dict))

    # Run the method based on the supplied arguments
    main = Main(opts)
    method = getattr(main, args["command"])
    kwargs = {k: args[k] for k in typing.get_type_hints(method).keys()}
    try:
        method(**kwargs)
    except RuntimeError as e:
        print("Error:", e, file=sys.stderr)
