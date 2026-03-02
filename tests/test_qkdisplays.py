import pytest
import time
import json
import i3ipc
from threading import Thread

from qkdisplays.main import Displays, UnixServer, Opts, Main, get_config

from .conftest import get_current_output_state, get_outputs


def test_get_config(monkeypatch, tmp_path):
    config_file = tmp_path / "qkdisplays.json"
    config_file.write_text(json.dumps({"allow_reorg": False}))

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    result = get_config()

    assert result.allow_reorg is False


def test_show_and_close(sway_instance):
    main = Main()
    show_thread = Thread(target=main.show, daemon=True)
    show_thread.start()

    # Wait for server to start
    for _ in range(10):
        if UnixServer.is_running():
            break
        time.sleep(0.1)
    else:
        pytest.fail("Unix server did not start")

    assert UnixServer.is_running()

    main.close()
    show_thread.join(timeout=2)
    assert not show_thread.is_alive()
    assert not UnixServer.is_running()


def test_move(sway_instance):
    i3 = i3ipc.Connection()

    outputs_before = get_outputs(i3, sort_by_pos=True)

    # Focus on the middle display (index 2, which is the 3rd display)
    i3.command(f"focus output {outputs_before[2].name}")
    time.sleep(0.1)  # give sway time

    # Move left
    assert Displays(Opts()).move("left")

    outputs_after = get_outputs(i3, sort_by_pos=True)

    # old display at index 2 should now be at index 1
    assert outputs_after[1].name == outputs_before[2].name
    # old display at index 1 should now be at index 2
    assert outputs_after[2].name == outputs_before[1].name

    # Move right (back to original)
    assert Displays(Opts()).move("right")
    outputs_final = get_outputs(i3, sort_by_pos=True)
    assert [o.name for o in outputs_final] == [o.name for o in outputs_before]


def test_move_boundary(sway_instance):
    i3 = i3ipc.Connection()
    outputs_before = get_outputs(i3, sort_by_pos=True)

    # Focus on leftmost display
    i3.command(f"focus output {outputs_before[0].name}")
    time.sleep(0.1)  # give sway time

    # Try to move left
    assert not Displays(Opts()).move("left")
    outputs_after = get_outputs(i3, sort_by_pos=True)
    assert [o.name for o in outputs_after] == [o.name for o in outputs_before]

    # Focus on rightmost display
    i3.command(f"focus output {outputs_before[-1].name}")
    time.sleep(0.1)  # give sway time

    # Try to move right
    assert not Displays(Opts()).move("right")
    outputs_after = get_outputs(i3, sort_by_pos=True)
    assert [o.name for o in outputs_after] == [o.name for o in outputs_before]


def test_focus(sway_instance):
    i3 = i3ipc.Connection()

    outputs = get_outputs(i3, sort_by_pos=True)
    Displays(Opts()).focus(3)

    focused_output = next(o for o in i3.get_outputs() if o.focused)
    assert focused_output.name == outputs[2].name

    # Test invalid focus
    Displays(Opts()).focus(0)
    focused_output_after_invalid = next(o for o in i3.get_outputs() if o.focused)
    assert focused_output_after_invalid.name == focused_output.name

    Displays(Opts()).focus(6)
    focused_output_after_invalid = next(o for o in i3.get_outputs() if o.focused)
    assert focused_output_after_invalid.name == focused_output.name


def test_place(sway_instance):
    i3 = i3ipc.Connection()

    outputs_before = get_outputs(i3, sort_by_pos=True)

    # Focus on display 1
    i3.command(f"focus output {outputs_before[0].name}")
    time.sleep(0.1)  # give sway time

    # Place it at position 3
    assert Displays(Opts()).place(3)

    outputs_after = get_outputs(i3, sort_by_pos=True)

    # The old display 1 (outputs_before[0]) should now be at the 3rd position.
    assert outputs_after[2].name == outputs_before[0].name
    # The old display 3 (outputs_before[2]) should now be at the 1st position.
    assert outputs_after[0].name == outputs_before[2].name
    # The old display 2 (outputs_before[1]) should be at the 2nd position.
    assert outputs_after[1].name == outputs_before[1].name


def test_reorg_disallowed_misconfigured_outputs(sway_instance):
    i3 = i3ipc.Connection()
    outputs = get_outputs(i3, sort_by_pos=True)
    output_names = [o.name for o in outputs]
    output_width = outputs[0].rect.width # Assuming all have same width

    # Non-contiguous X positions, allow_reorg=False
    i3.command(f"output {output_names[0]} pos 0 0")
    i3.command(f"output {output_names[1]} pos {output_width + 100} 0") # Create a gap
    i3.command(f"output {output_names[2]} pos {2 * output_width + 200} 0")
    i3.command(f"output {output_names[3]} pos {3 * output_width + 300} 0")
    i3.command(f"output {output_names[4]} pos {4 * output_width + 400} 0")

    with pytest.raises(RuntimeError, match="Outputs are not contiguous from left to right"):
        Displays(Opts(allow_reorg=False, strict_y=False))

    # Y-misaligned positions, strict_y=True, allow_reorg=False
    i3.command(f"output {output_names[0]} pos 0 0")
    i3.command(f"output {output_names[1]} pos {output_width} 100") # Different Y
    i3.command(f"output {output_names[2]} pos {2 * output_width} 0")
    i3.command(f"output {output_names[3]} pos {3 * output_width} 100")
    i3.command(f"output {output_names[4]} pos {4 * output_width} 0")

    with pytest.raises(RuntimeError, match="Outputs are not all in the same y position"):
        Displays(Opts(allow_reorg=False, strict_y=True))


def test_reorg_allowed_misconfigured_outputs(sway_instance):
    i3 = i3ipc.Connection()
    outputs = get_outputs(i3, sort_by_pos=True)
    output_names = [o.name for o in outputs]
    output_width = outputs[0].rect.width

    # Configure outputs to be non-contiguous AND Y-misaligned
    i3.command(f"output {output_names[0]} pos 0 0")
    i3.command(f"output {output_names[1]} pos {output_width + 50} 100") # Gap and Y-offset
    i3.command(f"output {output_names[2]} pos {2 * output_width + 100} 0")
    i3.command(f"output {output_names[3]} pos {3 * output_width + 150} 100")
    i3.command(f"output {output_names[4]} pos {4 * output_width + 200} 0")

    # Run Displays with allow_reorg=True and strict_y=True
    Displays(Opts(allow_reorg=True, strict_y=True))

    # Verify Sway output positions
    outputs_after_reorg = get_outputs(i3, sort_by_pos=True)
    assert len(outputs_after_reorg) == 5
    expected_x_sway = 0
    for i, output_sway in enumerate(outputs_after_reorg):
        assert output_sway.rect.x == expected_x_sway
        assert output_sway.rect.y == 0
        expected_x_sway += output_sway.rect.width
        # Ensure they are sorted by X
        if i > 0:
            assert outputs_after_reorg[i].rect.x > outputs_after_reorg[i-1].rect.x


def test_reorg_allowed_y_misaligned_outputs(sway_instance):
    i3 = i3ipc.Connection()
    outputs = get_outputs(i3, sort_by_pos=True)
    output_names = [o.name for o in outputs]
    output_width = outputs[0].rect.width # Assuming all have same width

    # Configure outputs to be contiguous in X but Y-misaligned
    i3.command(f"output {output_names[0]} pos 0 0")
    i3.command(f"output {output_names[1]} pos {output_width} 100") # Y-offset
    i3.command(f"output {output_names[2]} pos {2 * output_width} 0")
    i3.command(f"output {output_names[3]} pos {3 * output_width} 100")
    i3.command(f"output {output_names[4]} pos {4 * output_width} 0")
    time.sleep(0.1) # Give sway time to apply changes

    # Run Displays with allow_reorg=True and strict_y=True
    # This should trigger the _reorg_outputs with reorg_x=False path
    Displays(Opts(allow_reorg=True, strict_y=True))

    # Verify Sway output positions
    outputs_after_reorg = get_outputs(i3, sort_by_pos=True)
    assert len(outputs_after_reorg) == 5
    expected_x = 0
    for i, output_sway in enumerate(outputs_after_reorg):
        assert output_sway.rect.x == expected_x
        assert output_sway.rect.y == 0 # All should be moved to y=0
        expected_x += output_sway.rect.width
        # Ensure they are sorted by X
        if i > 0:
            assert outputs_after_reorg[i].rect.x > outputs_after_reorg[i-1].rect.x


def test_stress_multiple_actions_varied_sizes(sway_instance):
    i3 = i3ipc.Connection()

    # Define outputs with varying sizes
    output_configs = [
        ("HEADLESS-1", 600, 500),
        ("HEADLESS-2", 800, 500),
        ("HEADLESS-3", 700, 500),
        ("HEADLESS-4", 900, 500),
        ("HEADLESS-5", 500, 500),
    ]
    output_names = [cfg[0] for cfg in output_configs]

    # Initial setup: position outputs contiguously at y=0
    current_x = 0
    for name, width, height in output_configs:
        i3.command(f"output {name} pos {current_x} 0 res {width}x{height}")
        current_x += width

    # Start with the first output focused
    i3.command(f"focus output {output_names[0]}")
    time.sleep(0.1)  # give sway time

    # Action 1: place HEADLESS-1 (currently focused) to position 4
    # Original order (by x-pos): H1 H2 H3 H4 H5
    # H1 is at index 0. We want to place it at index 3 (4th position).
    # This means H1 swaps with H4.
    # New logical order: H4 H2 H3 H1 H5
    displays_instance = Displays(Opts())
    assert displays_instance.place(4)

    state_after_place = get_current_output_state(i3)
    expected_after_place = []
    # Calculate expected positions based on new order: H4, H2, H3, H1, H5
    new_order_names = [
        output_names[3],
        output_names[1],
        output_names[2],
        output_names[0],
        output_names[4],
    ]
    # Get widths for the new order
    new_order_widths = [
        output_configs[3][1], # H4 width
        output_configs[1][1], # H2 width
        output_configs[2][1], # H3 width
        output_configs[0][1], # H1 width
        output_configs[4][1]  # H5 width
    ]

    current_x = 0
    for i, name in enumerate(new_order_names):
        width = new_order_widths[i]
        height = 500 # All heights are 500
        expected_after_place.append((name, current_x, 0, width, height))
        current_x += width
    assert state_after_place == expected_after_place

    # Action 2: move focused display (HEADLESS-1, now at position 4) left twice
    # Current order (by x-pos): H4 H2 H3 H1 H5
    # H1 is at index 3.
    # Move left once: H4 H2 H1 H3 H5 (H1 swaps with H3)
    # Move left twice: H4 H1 H2 H3 H5 (H1 swaps with H2)
    # H1 should end up at index 1 (2nd position)
    displays_instance = Displays(Opts()) # Re-initialize to get updated internal state
    assert displays_instance.move("left") # H1 (idx 3) swaps with H3 (idx 2) -> H4 H2 H1 H3 H5
    displays_instance = Displays(Opts()) # Re-initialize
    assert displays_instance.move("left") # H1 (idx 2) swaps with H2 (idx 1) -> H4 H1 H2 H3 H5

    state_after_move = get_current_output_state(i3)
    expected_after_move = []
    # Calculate expected positions based on new order: H4, H1, H2, H3, H5
    final_order_names = [
        output_names[3],
        output_names[0],
        output_names[1],
        output_names[2],
        output_names[4],
    ]
    # Get widths for the final order
    final_order_widths = [
        output_configs[3][1], # H4 width
        output_configs[0][1], # H1 width
        output_configs[1][1], # H2 width
        output_configs[2][1], # H3 width
        output_configs[4][1]  # H5 width
    ]

    current_x = 0
    for i, name in enumerate(final_order_names):
        width = final_order_widths[i]
        height = 500
        expected_after_move.append((name, current_x, 0, width, height))
        current_x += width
    assert state_after_move == expected_after_move

    # Action 3: focus on the 4th display (which should be HEADLESS-3)
    # Current order (by x-pos): H4 H1 H2 H3 H5
    # 4th display is H3 (index 3)
    displays_instance = Displays(Opts()) # Re-initialize
    displays_instance.focus(4)

    focused_output_name_final = next(o.name for o in i3.get_outputs() if o.focused)
    assert focused_output_name_final == output_names[2] # HEADLESS-3


@pytest.mark.parametrize("sway_instance", [1], indirect=True)
def test_single_display_no_move(sway_instance):
    displays = Displays(Opts())

    # Assert that move operations return False (no movement)
    assert not displays.move("left")
    assert not displays.move("right")

    # Assert that place operations return False (no movement or invalid target)
    assert not displays.place(1) # Placing to self
    assert not displays.place(2) # Invalid target number


@pytest.mark.parametrize("sway_instance", [0], indirect=True)
def test_no_active_displays(sway_instance):
    # Instantiating Displays should not raise an error
    Displays(Opts())


@pytest.mark.parametrize("sway_instance", [5], indirect=True)
def test_move_with_disabled_middle_output_reorg_allowed(sway_instance):
    i3 = i3ipc.Connection()
    initial_outputs_active = get_outputs(i3, sort_by_pos=True)
    output_names = [o.name for o in initial_outputs_active]
    output_width = initial_outputs_active[0].rect.width # Assuming all have same width
    output_height = initial_outputs_active[0].rect.height # Assuming all have same height

    # Disable the middle output (HEADLESS-3, which is at index 2 in the sorted
    # list)
    middle_output_name = output_names[2]
    i3.command(f"output {middle_output_name} disable")
    time.sleep(0.1) # Give sway time to process

    # Instantiate Displays with allow_reorg=True.
    # This should re-arrange the *active* outputs to be contiguous.
    # The active outputs will be H1, H2, H4, H5.
    Displays(Opts(allow_reorg=True))

    # Verify the state after initial reorg by Displays
    state_after_initial_reorg = get_current_output_state(i3)
    expected_initial_reorg_names = [
        output_names[0],
        output_names[1],
        output_names[3],
        output_names[4],
    ]
    expected_initial_reorg_state = []
    current_x = 0
    for name in expected_initial_reorg_names:
        expected_initial_reorg_state.append(
            (name, current_x, 0, output_width, output_height)
        )
        current_x += output_width
    assert state_after_initial_reorg == expected_initial_reorg_state

    # Focus on the output before the disabled one (HEADLESS-2, which is now at
    # index 1 in the active list)
    i3.command(f"focus output {output_names[1]}") # Focus HEADLESS-2
    time.sleep(0.1) # Give sway time to process

    # Move right (H2 should swap with H4)
    assert Displays(Opts()).move("right")

    # Verify the final state of active outputs
    state_after_move = get_current_output_state(i3)
    # Expected order of active outputs: H1, H4, H2, H5
    expected_final_order_names = [
        output_names[0],
        output_names[3],
        output_names[1],
        output_names[4],
    ]
    expected_final_state = []
    current_x = 0
    for name in expected_final_order_names:
        expected_final_state.append(
            (name, current_x, 0, output_width, output_height)
        )
        current_x += output_width
    assert state_after_move == expected_final_state

    # Ensure the disabled output is still disabled and not in the active list
    all_outputs_final = i3.get_outputs()
    disabled_output_final = next(
        o for o in all_outputs_final if o.name == middle_output_name
    )
    assert not disabled_output_final.active


def test_set_scale(sway_instance):
    i3 = i3ipc.Connection()

    # Helper to check contiguity of outputs
    def assert_outputs_contiguous():
        outputs_state = get_current_output_state(i3)

        for i in range(1, len(outputs_state)):
            prev_output = outputs_state[i-1]
            current_output = outputs_state[i]
            # prev_output[1] is x, prev_output[3] is width
            expected_x = prev_output[1] + prev_output[3]
            # Check if current output's x is approximately where it should be
            # The tolerance of 1 pixel is based on the original _sort_outputs
            # logic (diff > 1 or diff < 1)
            assert current_output[1] == pytest.approx(expected_x, abs=1)

    # Test relative increase
    Displays(Opts()).set_scale("+0.1")
    focused_output_after_plus = next(o for o in i3.get_outputs() if o.focused)
    assert focused_output_after_plus.scale == pytest.approx(1.1)
    assert_outputs_contiguous()

    # Test relative decrease
    Displays(Opts()).set_scale("-0.05")
    focused_output_after_minus = next(o for o in i3.get_outputs() if o.focused)
    assert focused_output_after_minus.scale == pytest.approx(1.05)
    assert_outputs_contiguous()

    # Test absolute set
    Displays(Opts()).set_scale("1.32")
    focused_output_after_absolute = next(o for o in i3.get_outputs() if o.focused)
    assert focused_output_after_absolute.scale == pytest.approx(1.32, rel=0.01)
    assert_outputs_contiguous()

    # Test setting scale below 1 (should cap at 1)
    Displays(Opts()).set_scale("-100") # Large negative to ensure it goes below 1
    focused_output_after_min_cap = next(o for o in i3.get_outputs() if o.focused)
    assert focused_output_after_min_cap.scale == pytest.approx(1.0)
    assert_outputs_contiguous()
