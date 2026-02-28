import json
import threading
import time

import pytest
import i3ipc

from qkdisplays.main import Displays, Main, Opts

from .conftest import get_current_output_state, get_outputs


@pytest.fixture(autouse=True)
def isolated_state_dir(tmp_path, monkeypatch):
    """Point XDG_DATA_HOME to a temp dir so tests don't pollute."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))


@pytest.fixture(autouse=True)
def unique_monitor_ids(monkeypatch):
    """
    Headless sway outputs all report make/model/serial as "Unknown",
    so _get_monitor_id returns the same ID for every monitor. Patch
    it to use the output name instead, which is unique per headless
    output.
    """
    monkeypatch.setattr(
        Displays,
        "_get_monitor_id",
        staticmethod(lambda output: output.name),
    )


def test_save_and_restore_layout(sway_instance):
    i3 = i3ipc.Connection()
    outputs_before = get_outputs(i3, sort_by_pos=True)
    names_before = [o.name for o in outputs_before]

    # Focus on the middle display and move it left to rearrange
    i3.command(f"focus output {outputs_before[2].name}")
    time.sleep(0.1)

    displays = Displays(Opts())
    assert displays.move("left")

    outputs_after_move = get_outputs(i3, sort_by_pos=True)
    names_after_move = [o.name for o in outputs_after_move]
    assert names_after_move != names_before

    # Save the rearranged state
    displays = Displays(Opts())
    displays.save_state()

    # Move it back to original order
    i3.command(f"focus output {outputs_before[2].name}")
    time.sleep(0.1)
    displays = Displays(Opts())
    assert displays.move("right")

    outputs_reset = get_outputs(i3, sort_by_pos=True)
    assert [o.name for o in outputs_reset] == names_before

    # Restore from saved state
    displays = Displays(Opts())
    scale_restored, layout_restored = displays.restore_state()
    assert not scale_restored
    assert layout_restored

    # Verify the layout matches the saved rearranged order
    outputs_restored = get_outputs(i3, sort_by_pos=True)
    assert [o.name for o in outputs_restored] == names_after_move


def test_save_and_restore_scale(sway_instance):
    i3 = i3ipc.Connection()

    # Change scale of the focused display
    displays = Displays(Opts())
    displays.set_scale("1.5")

    focused = next(o for o in i3.get_outputs() if o.focused)
    assert focused.scale == pytest.approx(1.5)

    # Save the state with the new scale
    displays = Displays(Opts())
    displays.save_state()

    # Reset scale back to 1.0
    displays = Displays(Opts())
    displays.set_scale("1.0")

    focused = next(o for o in i3.get_outputs() if o.focused)
    assert focused.scale == pytest.approx(1.0)

    # Restore from saved state
    displays = Displays(Opts())
    scale_restored, layout_restored = displays.restore_state()
    assert scale_restored
    assert not layout_restored

    # Verify the scale was restored
    focused = next(o for o in i3.get_outputs() if o.focused)
    assert focused.scale == pytest.approx(1.5)


def test_restore_no_matching_layout(sway_instance):
    i3 = i3ipc.Connection()

    # Save state with 5 monitors
    displays = Displays(Opts())
    displays.save_state()

    # Disable one monitor so the set changes
    outputs = get_outputs(i3, sort_by_pos=True)
    i3.command(f"output {outputs[2].name} disable")
    time.sleep(0.1)

    # Get the state of the remaining 4 monitors before restore
    state_before = get_current_output_state(i3)

    # Try to restore -- should not match because monitor set
    # differs
    displays = Displays(Opts())
    scale_restored, layout_restored = displays.restore_state()
    assert not scale_restored
    assert not layout_restored

    # Verify monitors did not change
    state_after = get_current_output_state(i3)
    assert state_after == state_before


def test_save_and_restore_scale_and_layout(sway_instance):
    i3 = i3ipc.Connection()
    outputs_before = get_outputs(i3, sort_by_pos=True)
    names_before = [o.name for o in outputs_before]

    # Rearrange: move middle display left
    i3.command(f"focus output {outputs_before[2].name}")
    time.sleep(0.1)
    displays = Displays(Opts())
    assert displays.move("left")

    outputs_rearranged = get_outputs(i3, sort_by_pos=True)
    names_rearranged = [o.name for o in outputs_rearranged]

    # Change scale of the focused display
    displays = Displays(Opts())
    displays.set_scale("1.5")

    focused = next(o for o in i3.get_outputs() if o.focused)
    focused_name = focused.name
    assert focused.scale == pytest.approx(1.5)

    # Save the state with both changes
    displays = Displays(Opts())
    displays.save_state()

    # Reset: move back to original order and reset scale
    i3.command(f"focus output {outputs_before[2].name}")
    time.sleep(0.1)
    displays = Displays(Opts())
    displays.move("right")

    i3.command(f"focus output {focused_name}")
    time.sleep(0.1)
    displays = Displays(Opts())
    displays.set_scale("1.0")

    outputs_reset = get_outputs(i3, sort_by_pos=True)
    assert [o.name for o in outputs_reset] == names_before

    # Restore from saved state
    displays = Displays(Opts())
    scale_restored, layout_restored = displays.restore_state()
    assert scale_restored
    assert layout_restored

    # Verify layout matches saved rearranged order
    outputs_final = get_outputs(i3, sort_by_pos=True)
    assert [o.name for o in outputs_final] == names_rearranged

    # Verify scale was restored on the correct monitor
    restored_output = next(
        o for o in i3.get_outputs() if o.name == focused_name
    )
    assert restored_output.scale == pytest.approx(1.5)


def test_autosave_on_move(sway_instance):
    i3 = i3ipc.Connection()
    outputs_before = get_outputs(i3, sort_by_pos=True)

    # Focus on middle display and move left with autosave
    i3.command(f"focus output {outputs_before[2].name}")
    time.sleep(0.1)

    displays = Displays(Opts(autosave=True))
    assert displays.move("left")

    outputs_after_move = get_outputs(i3, sort_by_pos=True)
    names_after_move = [o.name for o in outputs_after_move]

    # Reset layout manually via i3ipc commands
    output_width = outputs_before[0].rect.width
    for idx, output in enumerate(outputs_before):
        i3.command(f"output {output.name} pos {idx * output_width} 0")

    outputs_reset = get_outputs(i3, sort_by_pos=True)
    assert [o.name for o in outputs_reset] == [o.name for o in outputs_before]

    # Restore from autosaved state (without autosave flag)
    displays = Displays(Opts())
    scale_restored, layout_restored = displays.restore_state()
    assert layout_restored

    outputs_restored = get_outputs(i3, sort_by_pos=True)
    assert [o.name for o in outputs_restored] == names_after_move


def test_autosave_on_set_scale(sway_instance):
    i3 = i3ipc.Connection()

    # Set scale with autosave enabled
    displays = Displays(Opts(autosave=True))
    displays.set_scale("+0.5")

    focused = next(o for o in i3.get_outputs() if o.focused)
    assert focused.scale == pytest.approx(1.5)

    # Reset scale directly
    i3.command(f"output {focused.name} scale 1.0")

    focused = next(o for o in i3.get_outputs() if o.focused)
    assert focused.scale == pytest.approx(1.0)

    # Restore from autosaved state
    displays = Displays(Opts())
    scale_restored, layout_restored = displays.restore_state()
    assert scale_restored
    assert not layout_restored

    focused = next(o for o in i3.get_outputs() if o.focused)
    assert focused.scale == pytest.approx(1.5)


@pytest.mark.parametrize("sway_instance", [1], indirect=True)
def test_save_single_display_no_layout(sway_instance):
    i3 = i3ipc.Connection()

    # Save state with a single monitor
    displays = Displays(Opts())
    displays.save_state()

    # Verify layouts list is empty in the state file
    state_path = Displays._get_state_path()
    with open(state_path) as f:
        state_data = json.load(f)
    assert state_data["layouts"] == []

    # Restore should do nothing for layout
    displays = Displays(Opts())
    scale_restored, layout_restored = displays.restore_state()
    assert not scale_restored
    assert not layout_restored

    # Scale saving still works for a single monitor: change scale,
    # save, reset, restore
    displays = Displays(Opts())
    displays.set_scale("1.5")

    focused = next(o for o in i3.get_outputs() if o.focused)
    assert focused.scale == pytest.approx(1.5)

    displays = Displays(Opts())
    displays.save_state()

    displays = Displays(Opts())
    displays.set_scale("1.0")

    focused = next(o for o in i3.get_outputs() if o.focused)
    assert focused.scale == pytest.approx(1.0)

    displays = Displays(Opts())
    scale_restored, layout_restored = displays.restore_state()
    assert scale_restored
    assert not layout_restored

    focused = next(o for o in i3.get_outputs() if o.focused)
    assert focused.scale == pytest.approx(1.5)


def test_load_malformed_state_file(sway_instance):
    # Write invalid JSON to the state file path
    state_path = Displays._get_state_path()
    with open(state_path, "w") as f:
        f.write("{invalid json content!!!")

    with pytest.raises(RuntimeError, match="malformed"):
        Displays._load_state()


def test_auto_restore(sway_instance, monkeypatch):
    i3 = i3ipc.Connection()
    outputs = get_outputs(i3, sort_by_pos=True)

    # Disable one output
    disabled_output = outputs[4]
    i3.command(f"output {disabled_output.name} disable")

    # Rearrange the 4 remaining outputs
    active = get_outputs(i3, sort_by_pos=True)
    i3.command(f"focus output {active[2].name}")
    displays = Displays(Opts())
    assert displays.place(1)

    # Save the 4-monitor layout
    displays = Displays(Opts())
    displays.save_state()
    layout_4 = [o.name for o in get_outputs(i3, sort_by_pos=True)]

    # Re-enable the disabled output
    i3.command(f"output {disabled_output.name} enable")

    # Rearrange the full 5-monitor set
    active = get_outputs(i3, sort_by_pos=True)
    i3.command(f"focus output {active[2].name}")
    displays = Displays(Opts())
    assert displays.move("left")

    # Save the 5-monitor layout
    displays = Displays(Opts())
    displays.save_state()
    layout_5 = [o.name for o in get_outputs(i3, sort_by_pos=True)]

    # Run auto_restore in a background thread.
    # _i3_main_loop installs a SIGINT handler which only works on the main
    # thread, so patch it to call i3.main() directly for the test.
    monkeypatch.setattr(
        Displays,
        "_i3_main_loop",
        staticmethod(lambda i3: i3.main()),
    )
    # Wrap auto_restore to swallow FileNotFoundError so the thread exits
    # cleanly when sway tears down at the end of the test.
    def auto_restore_safe():
        try:
            Main(Opts()).auto_restore()
        except FileNotFoundError:
            pass
    thread = threading.Thread(target=auto_restore_safe, daemon=True)
    thread.start()

    # Disable the previously re-enabled output
    i3.command(f"output {disabled_output.name} disable")
    time.sleep(0.5)

    # Verify the 4-monitor layout was restored
    actual_4 = [o.name for o in get_outputs(i3, sort_by_pos=True)]
    assert actual_4 == layout_4

    # Ee-enable the output
    i3.command(f"output {disabled_output.name} enable")
    time.sleep(0.5)

    # Verify the 5-monitor layout was restored
    actual_5 = [o.name for o in get_outputs(i3, sort_by_pos=True)]
    assert actual_5 == layout_5
