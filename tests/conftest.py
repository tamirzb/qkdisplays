import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

import i3ipc
import pytest

# Get the directory of the current test file
TEST_DIR = Path(__file__).parent
SWAY_CONFIG = TEST_DIR / "sway_config"


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
            text=True,  # Decode stdout/stderr as text
            env=sway_env,
            bufsize=1,  # Line-buffered output for easier reading
        )
        # For pyright to understand stdout always exists
        assert sway_process.stdout is not None

        # Wait for the READY message
        ready_message = "*@*READY*@*"
        wayland_display = None
        start_time = time.time()
        timeout = 15

        # Read stdout line by line until the ready message is found
        for line in iter(sway_process.stdout.readline, ""):
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
            sway_process.wait(timeout=5)  # Give it a moment to die
        pytest.fail(f"Sway instance setup failed: {e}")

    finally:
        # Tear down sway process
        if sway_process and sway_process.poll() is None:
            try:
                conn = i3ipc.Connection()
                conn.command("exit")
                conn.main_quit()  # Disconnect from IPC

                # Wait for Sway to exit
                sway_process.wait(timeout=15)
            except i3ipc.Exception as e:
                print(f"i3ipc error during exit: {e}")
                # If i3ipc fails, Sway might still be running,
                # try to kill it
                if sway_process.poll() is None:
                    print("i3ipc failed, killing Sway process.")
                    sway_process.kill()
                    sway_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                print(
                    "Sway did not exit gracefully within 15 "
                    "seconds. Killing it."
                )
                sway_process.kill()
                sway_process.wait(timeout=5)
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


def get_outputs(i3, sort_by_pos=False):
    """Helper to get active outputs."""
    outputs = [o for o in i3.get_outputs() if o.active]
    if sort_by_pos:
        return sorted(outputs, key=lambda o: o.rect.x)
    return sorted(outputs, key=lambda o: o.name)


def get_current_output_state(i3):
    """
    Helper to get active outputs and their full state, sorted by
    x-position.
    """
    sorted_outputs = get_outputs(i3, sort_by_pos=True)
    return [
        (o.name, o.rect.x, o.rect.y, o.rect.width, o.rect.height)
        for o in sorted_outputs
    ]
