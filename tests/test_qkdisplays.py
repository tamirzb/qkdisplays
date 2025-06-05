import pytest
import subprocess
import os
import time
import re
import tempfile
from pathlib import Path
import i3ipc
from threading import Thread

from qkdisplays.main import Displays, UnixServer, Opts, Main

# Get the directory of the current test file
TEST_DIR = Path(__file__).parent
SWAY_CONFIG = TEST_DIR / "sway_config"

# --- Pytest Fixture for Sway Instance ---
@pytest.fixture(scope="function")
def sway_instance(request):
    """
    Launches a Sway instance for each test, waits for it to be ready,
    sets environment variables, and tears it down.
    """
    # By default run with 5 displays unless requested otherwise
    displays_number = getattr(request, "param", 5)

    sway_process = None
    original_swaysock = os.environ.get("SWAYSOCK")
    original_wayland_display = os.environ.get("WAYLAND_DISPLAY")
    try:
        # Prepare environment for Sway subprocess
        sway_socket = tempfile.mktemp(prefix="pytest_sway", suffix=".sock")
        sway_env = os.environ.copy()
        sway_env["SWAYSOCK"] = sway_socket
        sway_env["WLR_BACKENDS"] = "headless"
        sway_env["WLR_LIBINPUT_NO_DEVICES"] = "1"
        sway_env["WLR_HEADLESS_OUTPUTS"] = str(displays_number)

        # Execute sway
        sway_process = subprocess.Popen(
            ["sway", "-c", str(SWAY_CONFIG)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True, # Decode stdout/stderr as text
            env=sway_env,
            bufsize=1 # Line-buffered output for easier reading
        )
        # For pyright to understand stdout always exists
        assert sway_process.stdout is not None

        # Wait for the READY message
        ready_message = "*@*READY*@*"
        wayland_display = None
        start_time = time.time()
        timeout = 15

        # Read stdout line by line until the ready message is found
        for line in iter(sway_process.stdout.readline, ''):
            if ready_message in line:
                match = re.search(r"WAYLAND_DISPLAY=([\w-]+)", line)
                if match:
                    wayland_display = match.group(1)
                    break
            if time.time() - start_time > timeout:
                raise TimeoutError(
                    f"Sway did not become ready in {timeout} seconds."
                )
        else:
            raise RuntimeError(
                "Sway process exited before printing ready message."
            )

        if not wayland_display:
            raise RuntimeError(
                "Could not extract WAYLAND_DISPLAY from Sway output."
            )

        # Set environment variables for test environment
        os.environ["SWAYSOCK"] = sway_socket
        os.environ["I3SOCK"] = sway_socket
        os.environ["WAYLAND_DISPLAY"] = wayland_display

        # Yield control to the test function
        yield

    except Exception as e:
        # If any error occurs during setup, ensure Sway is terminated
        if sway_process and sway_process.poll() is None:
            print(f"Error during Sway setup, killing process: {e}")
            sway_process.kill()
            sway_process.wait(timeout=5) # Give it a moment to die
        pytest.fail(f"Sway instance setup failed: {e}")

    finally:
        # Tear down sway process
        if sway_process and sway_process.poll() is None:
            try:
                conn = i3ipc.Connection()
                conn.command("exit")
                conn.main_quit() # Disconnect from IPC

                # Wait for Sway to exit
                sway_process.wait(timeout=15)
            except i3ipc.Exception as e:
                print(f"i3ipc error during exit: {e}")
                # If i3ipc fails, Sway might still be running, try to kill it
                if sway_process.poll() is None:
                    print("i3ipc failed, killing Sway process.")
                    sway_process.kill()
                    sway_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                print(
                    "Sway did not exit gracefully within 15 seconds. Killing "
                    "it."
                )
                sway_process.kill()
                sway_process.wait(timeout=5) # Give it a moment to die
            except Exception as e:
                print(f"Unexpected error during Sway teardown: {e}")
                if sway_process.poll() is None:
                    print("Killing Sway process due to unexpected error.")
                    sway_process.kill()
                    sway_process.wait(timeout=5)

        # Restore original environment variables
        if original_swaysock is not None:
            os.environ["SWAYSOCK"] = original_swaysock
            os.environ["I3SOCK"] = original_swaysock
        else:
            if "SWAYSOCK" in os.environ:
                del os.environ["SWAYSOCK"]
            if "I3SOCK" in os.environ:
                del os.environ["I3SOCK"]
        if original_wayland_display is not None:
            os.environ["WAYLAND_DISPLAY"] = original_wayland_display
        else:
            if "WAYLAND_DISPLAY" in os.environ:
                del os.environ["WAYLAND_DISPLAY"]


def get_outputs(i3: i3ipc.Connection, sort_by_pos=False):
    """Helper to get active outputs."""
    outputs = [o for o in i3.get_outputs() if o.active]
    if sort_by_pos:
        return sorted(outputs, key=lambda o: o.rect.x)
    return sorted(outputs, key=lambda o: o.name)


def get_current_output_state(i3: i3ipc.Connection):
    """
    Helper to get active outputs and their full state, sorted by x-position.
    """
    sorted_outputs = get_outputs(i3, sort_by_pos=True)
    return [
        (o.name, o.rect.x, o.rect.y, o.rect.width, o.rect.height)
        for o in sorted_outputs
    ]


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
