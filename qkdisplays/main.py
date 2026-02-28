import argparse
import dataclasses
import json
import os
import re
import signal
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
    autosave: bool = False


class OptsOptional(construct_optional_dataclass(Opts)):
    pass


class Displays:
    """
    A class to include all business logic of managing displays (aka outputs)
    """

    @dataclasses.dataclass
    class OutputData:
        name: str
        monitor_id: str
        x: int
        y: int
        width: int
        height: int
        scale: float
        focused: bool

    @dataclasses.dataclass
    class SavedState:
        # Monitor ID -> scale
        scales: dict[str, float] = dataclasses.field(default_factory=dict)
        # List of layouts, ordered by monitor IDs, in left-to-right order
        layouts: list[list[str]] = dataclasses.field(default_factory=list)

    _allow_reorg: bool
    _strict_y: bool
    _autosave: bool
    _sorted_outputs: list[OutputData]

    def __init__(self, opts: Opts):
        self._allow_reorg = opts.allow_reorg
        self._strict_y = opts.strict_y
        self._autosave = opts.autosave
        self.calculate_outputs()

    @staticmethod
    def _get_monitor_id(output: i3ipc.OutputReply) -> str:
        """Get a unique identifier for a monitor."""
        return (
            f"{output.ipc_data['make']}"
            f"|{output.ipc_data['model']}"
            f"|{output.ipc_data['serial']}"
        )

    @staticmethod
    def _get_outputs_data() -> list[OutputData]:
        outputs = i3ipc.Connection().get_outputs()

        result = []
        for output in outputs:
            if not output.active:
                continue

            output_data = Displays.OutputData(
                output.name,
                Displays._get_monitor_id(output),
                output.rect.x,
                output.rect.y,
                output.rect.width,
                output.rect.height,
                output.scale,
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

    @staticmethod
    def _get_current_scale() -> float:
        """Finds the focused output and returns its scale"""
        outputs = i3ipc.Connection().get_outputs()
        for output in outputs:
            if output.focused:
                return output.scale
        raise RuntimeError("No focused output found to get scale from")

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
        self._sorted_outputs[left_index : right_index + 1] = new_order

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

        if self._autosave:
            state = self._load_state()
            self._update_state_layouts(state)
            self._save_state(state)

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

    def set_scale(self, scale_str: str):
        """
        Sets the scale of the currently focused output.
        If scale_str begins with '+' or '-', it's a relative change.
        Otherwise, it's an absolute value.
        """
        i3 = i3ipc.Connection()

        try:
            if scale_str.startswith(("+", "-")):
                new_scale = self._get_current_scale() + float(scale_str)
            else:
                new_scale = float(scale_str)
        except ValueError:
            raise ValueError(f"Invalid scale change value: {scale_str}")

        if new_scale < 1:
            # We assume a scale lower than 1 does not make sense
            new_scale = 1

        i3.command(f"output - scale {new_scale}")

        # Changing scale can make outputs non-contiguous, so if we are allowed
        # to move outputs then do so
        if self._allow_reorg:
            self.calculate_outputs()

        if self._autosave:
            state = self._load_state()
            for output_data in self._sorted_outputs:
                if output_data.focused:
                    state.scales[output_data.monitor_id] = new_scale
            self._save_state(state)

    @staticmethod
    def _get_state_path() -> str:
        """Get the path to the state JSON file."""
        xdg_data_home = os.getenv("XDG_DATA_HOME")
        if not xdg_data_home:
            home = os.getenv("HOME", "")
            xdg_data_home = os.path.join(home, ".local", "share")
        directory = os.path.join(xdg_data_home, "qkdisplays")
        os.makedirs(directory, exist_ok=True)
        return os.path.join(directory, "state.json")

    @staticmethod
    def _load_state() -> SavedState:
        """Load state from the state file."""
        path = Displays._get_state_path()
        if not os.path.exists(path):
            return Displays.SavedState()
        try:
            with open(path) as f:
                return Displays.SavedState(**json.load(f))
        except (json.JSONDecodeError, TypeError) as e:
            raise RuntimeError(f"State file {path} is malformed: {e}") from e

    @staticmethod
    def _save_state(state: SavedState) -> None:
        """Save state to the state file."""
        path = Displays._get_state_path()
        with open(path, "w") as f:
            json.dump(dataclasses.asdict(state), f, indent=4)

    def _update_state_layouts(self, state: SavedState) -> None:
        """Update state with current monitor ordering."""
        layout = [o.monitor_id for o in self._sorted_outputs]

        if len(layout) == 1:
            # No need to save a layout of just one monitor
            return

        layout_set = set(layout)
        for i, saved_layout in enumerate(state.layouts):
            if set(saved_layout) == layout_set:
                state.layouts[i] = layout
                return

        state.layouts.append(layout)

    def _restore_scales(self, state: SavedState) -> bool:
        """
        Restore saved scales for current monitors.
        Returns whether any scale was restored.
        """
        restored = False
        i3 = i3ipc.Connection()
        for output in self._sorted_outputs:
            if output.monitor_id in state.scales:
                saved_scale = state.scales[output.monitor_id]
                if saved_scale != output.scale:
                    i3.command(f"output {output.name} scale {saved_scale}")
                    output.scale = saved_scale
                    restored = True

        # Changing scale can make outputs non-contiguous, so if we are allowed
        # to move outputs then do so
        if restored and self._allow_reorg:
            self.calculate_outputs()

        return restored

    def _restore_layout(self, state: SavedState) -> bool:
        """
        Restore saved layout (monitor ordering) if the current set
        of monitors matches a saved layout. Repositions monitors
        contiguously according to the saved order.
        Returns whether layout was restored.
        """
        current_ids = {o.monitor_id for o in self._sorted_outputs}

        if len(current_ids) == 1:
            # No need to restore a layout of just one monitor
            return False

        saved_order = None
        for saved_layout in state.layouts:
            if set(saved_layout) == current_ids:
                saved_order = saved_layout
                break
        if saved_order is None:
            return False

        current_order = [o.monitor_id for o in self._sorted_outputs]
        if current_order == saved_order:
            return False

        # Build a lookup from monitor_id to OutputData
        by_id = {o.monitor_id: o for o in self._sorted_outputs}

        # Reposition contiguously in the saved order, anchored to the
        # current leftmost x position (outputs are already sorted left
        # to right, so index 0 is the leftmost).
        x = self._sorted_outputs[0].x
        y = self._sorted_outputs[0].y
        i3 = i3ipc.Connection()
        ordered = []
        for mid in saved_order:
            output = by_id[mid]
            if not self._strict_y:
                y = output.y
            i3.command(f"output {output.name} pos {x} {y}")
            output.x = x
            output.y = y
            x += output.width
            ordered.append(output)

        self._sorted_outputs = ordered
        return True

    def save_state(self) -> None:
        """Save the current scale and layout of all active monitors."""
        state = self._load_state()
        for output in self._sorted_outputs:
            state.scales[output.monitor_id] = output.scale
        self._update_state_layouts(state)
        self._save_state(state)

    def restore_state(self) -> tuple[bool, bool]:
        """
        Restore saved scale and layout for the current monitors.
        Returns a tuple of (scale_restored, layout_restored).
        """
        state = self._load_state()
        layout_restored = False
        if self._allow_reorg:
            layout_restored = self._restore_layout(state)
        scale_restored = self._restore_scales(state)
        return scale_restored, layout_restored

    @staticmethod
    def _i3_main_loop(i3: i3ipc.Connection) -> None:
        """
        Run i3.main() and propagate KeyboardInterrupt on Ctrl+C.
        """
        # i3.main() blocks on a raw socket recv(), which means SIGINT
        # interrupts the syscall but the resulting exception is caught
        # internally and main() returns normally rather than raising
        # KeyboardInterrupt. To work around this, we install a temporary SIGINT
        # handler that calls main_quit() (so the loop exits) and records the
        # interruption, then re-raises KeyboardInterrupt after main() returns.

        interrupted = False

        def on_sigint(signum: int, frame) -> None:
            nonlocal interrupted
            interrupted = True
            i3.main_quit()

        old_handler = signal.signal(signal.SIGINT, on_sigint)
        try:
            i3.main()
        finally:
            signal.signal(signal.SIGINT, old_handler)

        if interrupted:
            raise KeyboardInterrupt

    def wait_for_change(self) -> None:
        """
        Wait until there is a change with the active displays. Either a display
        is added or removed.
        """
        current_ids = {o.monitor_id for o in self._sorted_outputs}

        def on_output(conn: i3ipc.Connection, event) -> None:
            # The output event can be triggered for multiple reasons that are
            # irrelevant here (e.g. resolution change). So only finish waiting
            # if we see that the active output ids changed.
            new_ids = {o.monitor_id for o in self._get_outputs_data()}
            if new_ids != current_ids:
                conn.main_quit()

        i3 = i3ipc.Connection()
        i3.on(i3ipc.Event.OUTPUT, on_output)
        self._i3_main_loop(i3)

        self.calculate_outputs()


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

    def set_scale(self, scale: str):
        """
        Set the scale of the currently focused display. Can be an absolute
        value (e.g., "1.5") or a relative change (e.g., "+0.2", "-0.1").
        """
        Displays(self._opts).set_scale(scale)

    def save(self):
        """
        Save the current state of all displays.
        """
        Displays(self._opts).save_state()

    def restore(self):
        """
        Restore displays from saved state if the current monitors match a saved
        configuration. Restores both saved scales and layout, or just scale if
        allow-reorg is false.
        """
        _, layout_restored = Displays(self._opts).restore_state()
        if layout_restored:
            UnixServer.send("notify")

    def auto_restore(self):
        """
        Runs continuously. Automatically restores saved displays state
        ('qkdisplays restore') when the active displays change (either one is
        added or removed).
        """
        displays = Displays(self._opts)
        _, layout_restored = displays.restore_state()
        while True:
            if layout_restored:
                UnixServer.send("notify")
            displays.wait_for_change()
            _, layout_restored = displays.restore_state()


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
        "--autosave",
        action=argparse.BooleanOptionalAction,
        help="Automatically save state after every operation that "
        "changes display layout or scale",
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
