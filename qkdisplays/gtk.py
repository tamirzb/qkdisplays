"""
This module aims to encapsulate all GTK-related logic. By doing this, GTK
libraries can be loaded lazily. This in turn should allow commands that don't
require GTK to be able to run in environments that don't have it.
"""

import typing
from threading import Thread

import gi

from .types import Point

gi.require_version("Gtk", "3.0")
try:
    gi.require_version("GtkLayerShell", "0.1")
except ValueError:
    raise RuntimeError("GTK Layer Shell not found")

from gi.repository import Gtk, GLib, GtkLayerShell, Gdk  # noqa: E402


MonitorsLocations = typing.Iterable[Point]


class PopupWindow(Gtk.Window):
    """The popup window that displays on each monitor"""

    def __init__(self, monitor, text: str):
        super().__init__()

        label = Gtk.Label(label=text)
        self.add(label)

        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_monitor(self, monitor)


class GtkTools:
    _thread: Thread
    _windows: list[PopupWindow]

    def __init__(self):
        css = """
            window {
                background-color: rgba(255, 255, 255, 0.6);
                border: 5px solid rgba(200, 200, 200, 0.6);
                color: black;
                font-size: 100px;
            }

            label {
                padding: 30px;
                min-width: 100px;
            }
        """
        css_provider = Gtk.CssProvider()
        css_provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        self._thread = Thread(target=Gtk.main)
        self._windows = []

    def start_thread(self):
        self._thread.start()

    def _show_indicators(self, sorted_monitor_locations: MonitorsLocations):
        # Prepare Gdk monitor data
        monitors = {}
        # Sometimes trying to obtain Gdk's display straight after changing
        # output positions can cause it to have bad data. Seems that reopening
        # the display solves it.
        display = Gdk.Display.open(Gdk.Display.get_default().get_name())
        for i in range(display.get_n_monitors()):
            monitor = display.get_monitor(i)
            geometry = monitor.get_geometry()
            monitors[Point(geometry.x, geometry.y)] = monitor

        for i, location in enumerate(sorted_monitor_locations):
            window = PopupWindow(monitors[location], str(i + 1))
            window.show_all()
            self._windows.append(window)

    def show_indicators(self, sorted_monitor_locations: MonitorsLocations):
        """
        Show an indicator on each monitor.
        Receives the locations of each monitor, sorted by the number that
        should be displayed.
        """
        GLib.idle_add(self._show_indicators, sorted_monitor_locations)

    def refresh_indicators(self, sorted_monitor_locations: MonitorsLocations):
        """
        Show an indicator on each monitor.
        Receives the locations of each monitor, sorted by the number that
        should be displayed.
        """
        def _refresh_indicators():
            for window in self._windows:
                window.close()
            self._windows = []
            self._show_indicators(sorted_monitor_locations)

        GLib.idle_add(_refresh_indicators)

    def quit(self):
        GLib.idle_add(Gtk.main_quit)
        self._thread.join()
