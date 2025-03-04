# Copyright (c) 2021 Matt Colligan
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from __future__ import annotations

import asyncio
import contextlib
import os
import time
import typing
from collections import defaultdict

import pywayland
import pywayland.server
import wlroots.helper as wlroots_helper
import wlroots.wlr_types.virtual_keyboard_v1 as vkeyboard
import wlroots.wlr_types.virtual_pointer_v1 as vpointer
from pywayland import lib as wllib
from pywayland.protocol.wayland import WlSeat
from wlroots import xwayland
from wlroots.wlr_types import (
    DataControlManagerV1,
    DataDeviceManager,
    ExportDmabufManagerV1,
    ForeignToplevelManagerV1,
    GammaControlManagerV1,
    InputInhibitManager,
    OutputLayout,
    PointerGesturesV1,
    PrimarySelectionV1DeviceManager,
    RelativePointerManagerV1,
    ScreencopyManagerV1,
    Surface,
    XCursorManager,
    XdgOutputManagerV1,
    input_device,
    pointer,
    seat,
    xdg_decoration_v1,
)
from wlroots.wlr_types.cursor import Cursor, WarpMode
from wlroots.wlr_types.idle import Idle
from wlroots.wlr_types.idle_inhibit_v1 import IdleInhibitorManagerV1, IdleInhibitorV1
from wlroots.wlr_types.layer_shell_v1 import LayerShellV1, LayerShellV1Layer, LayerSurfaceV1
from wlroots.wlr_types.output_management_v1 import (
    OutputConfigurationHeadV1,
    OutputConfigurationV1,
    OutputManagerV1,
)
from wlroots.wlr_types.output_power_management_v1 import (
    OutputPowerManagementV1Mode,
    OutputPowerManagerV1,
    OutputPowerV1SetModeEvent,
)
from wlroots.wlr_types.pointer_constraints_v1 import PointerConstraintsV1, PointerConstraintV1
from wlroots.wlr_types.server_decoration import (
    ServerDecorationManager,
    ServerDecorationManagerMode,
)
from wlroots.wlr_types.xdg_shell import XdgShell, XdgSurface, XdgSurfaceRole
from xkbcommon import xkb

from libqtile import hook, log_utils
from libqtile.backend import base
from libqtile.backend.wayland import inputs, layer, window, wlrq, xdgwindow, xwindow
from libqtile.backend.wayland.output import Output
from libqtile.command.base import expose_command
from libqtile.log_utils import logger

if typing.TYPE_CHECKING:
    from typing import Any, Generator

    from pywayland.server import Listener
    from wlroots.wlr_types import Output as wlrOutput
    from wlroots.wlr_types.data_device_manager import Drag

    from libqtile import config
    from libqtile.core.manager import Qtile


class Core(base.Core, wlrq.HasListeners):
    supports_restarting: bool = False

    def __init__(self) -> None:
        """Setup the Wayland core backend"""
        self.qtile: Qtile | None = None

        # This is the window under the pointer
        self._hovered_window: window.WindowType | None = None
        # but this Internal receives keyboard input, e.g. via the Prompt widget.
        self.focused_internal: window.Internal | None = None

        # Log exceptions that are raised in Wayland callback functions.
        log_utils.init_log(
            logger.level,
            log_path=log_utils.get_default_log(),
            logger=pywayland.server.listener.logger,
        )

        self.fd: int | None = None
        self.display = pywayland.server.display.Display()
        self.event_loop = self.display.get_event_loop()
        (
            self.compositor,
            self._allocator,
            self.renderer,
            self.backend,
        ) = wlroots_helper.build_compositor(self.display)
        self.socket = self.display.add_socket()
        os.environ["WAYLAND_DISPLAY"] = self.socket.decode()
        logger.info("Starting core with WAYLAND_DISPLAY=%s", self.socket.decode())

        # These windows have not been mapped yet; they'll get managed when mapped
        self.pending_windows: set[window.WindowType] = set()

        # mapped_windows contains just regular windows
        self.mapped_windows: list[window.WindowType] = []  # Ascending in Z
        # stacked_windows also contains layer_shell windows from the current output
        self.stacked_windows: list[window.WindowType] = []  # Ascending in Z
        self._current_output: Output | None = None

        # set up inputs
        self.keyboards: list[inputs.Keyboard] = []
        self._pointers: list[inputs.Pointer] = []
        self.grabbed_keys: list[tuple[int, int]] = []
        DataDeviceManager(self.display)
        self.live_dnd: wlrq.Dnd | None = None
        DataControlManagerV1(self.display)
        self.seat = seat.Seat(self.display, "seat0")
        self.add_listener(self.seat.request_set_selection_event, self._on_request_set_selection)
        self.add_listener(
            self.seat.request_set_primary_selection_event, self._on_request_set_primary_selection
        )
        self.add_listener(self.seat.request_start_drag_event, self._on_request_start_drag)
        self.add_listener(self.seat.start_drag_event, self._on_start_drag)
        self.add_listener(self.backend.new_input_event, self._on_new_input)
        # Some devices are added early, so we need to remember to configure them
        self._pending_input_devices: list[inputs._Device] = []
        hook.subscribe.startup_complete(self._configure_pending_inputs)

        self._input_inhibit_manager = InputInhibitManager(self.display)
        self.add_listener(
            self._input_inhibit_manager.activate_event, self._on_input_inhibitor_activate
        )
        self.add_listener(
            self._input_inhibit_manager.deactivate_event, self._on_input_inhibitor_deactivate
        )
        self.exclusive_client: pywayland.server.Client | None = None

        # set up outputs
        self.outputs: list[Output] = []
        self.add_listener(self.backend.new_output_event, self._on_new_output)
        self.output_layout = OutputLayout()
        self.add_listener(self.output_layout.change_event, self._on_output_layout_change)
        self.output_manager = OutputManagerV1(self.display)
        self.add_listener(self.output_manager.apply_event, self._on_output_manager_apply)
        self.add_listener(self.output_manager.test_event, self._on_output_manager_test)
        self._blanked_outputs: set[Output] = set()

        # set up cursor
        self.cursor = Cursor(self.output_layout)
        self.cursor_manager = XCursorManager(24)
        self._gestures = PointerGesturesV1(self.display)
        self.add_listener(self.seat.request_set_cursor_event, self._on_request_cursor)
        self.add_listener(self.cursor.axis_event, self._on_cursor_axis)
        self.add_listener(self.cursor.frame_event, self._on_cursor_frame)
        self.add_listener(self.cursor.button_event, self._on_cursor_button)
        self.add_listener(self.cursor.motion_event, self._on_cursor_motion)
        self.add_listener(self.cursor.motion_absolute_event, self._on_cursor_motion_absolute)
        self.add_listener(self.cursor.pinch_begin, self._on_cursor_pinch_begin)
        self.add_listener(self.cursor.pinch_update, self._on_cursor_pinch_update)
        self.add_listener(self.cursor.pinch_end, self._on_cursor_pinch_end)
        self.add_listener(self.cursor.swipe_begin, self._on_cursor_swipe_begin)
        self.add_listener(self.cursor.swipe_update, self._on_cursor_swipe_update)
        self.add_listener(self.cursor.swipe_end, self._on_cursor_swipe_end)
        self.add_listener(self.cursor.hold_begin, self._on_cursor_hold_begin)
        self.add_listener(self.cursor.hold_end, self._on_cursor_hold_end)
        self._cursor_state = wlrq.CursorState()

        # set up shell
        self.xdg_shell = XdgShell(self.display)
        self.add_listener(self.xdg_shell.new_surface_event, self._on_new_xdg_surface)
        self.layer_shell = LayerShellV1(self.display)
        self.add_listener(self.layer_shell.new_surface_event, self._on_new_layer_surface)

        # Add support for additional protocols
        ExportDmabufManagerV1(self.display)
        XdgOutputManagerV1(self.display, self.output_layout)
        ScreencopyManagerV1(self.display)
        GammaControlManagerV1(self.display)
        output_power_manager = OutputPowerManagerV1(self.display)
        self.add_listener(
            output_power_manager.set_mode_event, self._on_output_power_manager_set_mode
        )
        self.idle = Idle(self.display)
        idle_ihibitor_manager = IdleInhibitorManagerV1(self.display)
        self.add_listener(idle_ihibitor_manager.new_inhibitor_event, self._on_new_idle_inhibitor)
        PrimarySelectionV1DeviceManager(self.display)
        virtual_keyboard_manager_v1 = vkeyboard.VirtualKeyboardManagerV1(self.display)
        self.add_listener(
            virtual_keyboard_manager_v1.new_virtual_keyboard_event,
            self._on_new_virtual_keyboard,
        )
        virtual_pointer_manager_v1 = vpointer.VirtualPointerManagerV1(self.display)
        self.add_listener(
            virtual_pointer_manager_v1.new_virtual_pointer_event,
            self._on_new_virtual_pointer,
        )
        xdg_decoration_manager_v1 = xdg_decoration_v1.XdgDecorationManagerV1.create(self.display)
        self.add_listener(
            xdg_decoration_manager_v1.new_toplevel_decoration_event,
            self._on_new_toplevel_decoration,
        )
        # wlr_server_decoration will be removed in a future version of wlroots
        server_decoration_manager = ServerDecorationManager.create(self.display)
        server_decoration_manager.set_default_mode(ServerDecorationManagerMode.SERVER)
        pointer_constraints_v1 = PointerConstraintsV1(self.display)
        self.add_listener(
            pointer_constraints_v1.new_constraint_event,
            self._on_new_pointer_constraint,
        )
        self.pointer_constraints: set[window.PointerConstraint] = set()
        self.active_pointer_constraint: window.PointerConstraint | None = None
        self._relative_pointer_manager_v1 = RelativePointerManagerV1(self.display)
        self.foreign_toplevel_manager_v1 = ForeignToplevelManagerV1.create(self.display)

        # Set up XWayland
        self._xwayland: xwayland.XWayland | None = None
        try:
            self._xwayland = xwayland.XWayland(self.display, self.compositor, True)
        except RuntimeError:
            logger.info("Failed to set up XWayland. Continuing without.")
        else:
            os.environ["DISPLAY"] = self._xwayland.display_name or ""
            logger.info("Set up XWayland with DISPLAY=%s", os.environ["DISPLAY"])
            self.add_listener(self._xwayland.ready_event, self._on_xwayland_ready)
            self.add_listener(self._xwayland.new_surface_event, self._on_xwayland_new_surface)

        # Start
        self.backend.start()

        # Place cursor in middle of centre output
        x = y = 0
        if box := self.output_layout.get_box():
            if output := self.output_layout.output_at(box.width / 2, box.height / 2):
                if box := self.output_layout.get_box(reference=output):
                    x = box.x + box.width / 2
                    y = box.y + box.height / 2
        self.warp_pointer(x, y)
        self.cursor_manager.set_cursor_image("left_ptr", self.cursor)

    @property
    def name(self) -> str:
        return "wayland"

    def finalize(self) -> None:
        self.finalize_listeners()
        self._poll()
        for kb in self.keyboards.copy():
            kb.finalize()
        for pt in self._pointers.copy():
            pt.finalize()
        for out in self.outputs.copy():
            out.finalize()

        if self._xwayland:
            self._xwayland.destroy()
        self.cursor_manager.destroy()
        self.cursor.destroy()
        self.output_layout.destroy()
        self.seat.destroy()
        self.backend.destroy()
        self.display.destroy()
        self.qtile = None

    @property
    def display_name(self) -> str:
        return self.socket.decode()

    def _on_request_set_selection(
        self, _listener: Listener, event: seat.RequestSetSelectionEvent
    ) -> None:
        self.seat.set_selection(event._ptr.source, event.serial)
        logger.debug("Signal: seat request_set_selection")

    def _on_request_set_primary_selection(
        self, _listener: Listener, event: seat.RequestSetPrimarySelectionEvent
    ) -> None:
        self.seat.set_primary_selection(event._ptr.source, event.serial)
        logger.debug("Signal: seat request_set_primary_selection")

    def _on_request_start_drag(
        self, _listener: Listener, event: seat.RequestStartDragEvent
    ) -> None:
        logger.debug("Signal: seat request_start_drag")

        if not self.live_dnd and self.seat.validate_pointer_grab_serial(
            event.origin, event.serial
        ):
            self.seat.start_pointer_drag(event.drag, event.serial)
        else:
            event.drag.source.destroy()

    def _on_start_drag(self, _listener: Listener, event: Drag) -> None:
        logger.debug("Signal: seat start_drag")
        self.live_dnd = wlrq.Dnd(self, event)

    def _on_new_input(self, _listener: Listener, wlr_device: input_device.InputDevice) -> None:
        logger.debug("Signal: backend new_input_event")

        device: inputs._Device
        if wlr_device.device_type == input_device.InputDeviceType.POINTER:
            device = self._add_new_pointer(wlr_device)
        elif wlr_device.device_type == input_device.InputDeviceType.KEYBOARD:
            device = self._add_new_keyboard(wlr_device)
        else:
            logger.info("New %s device", wlr_device.device_type.name)
            return

        capabilities = WlSeat.capability.pointer
        if self.keyboards:
            capabilities |= WlSeat.capability.keyboard
        self.seat.set_capabilities(capabilities)

        logger.info("New device: %s %s", *device.get_info())
        if self.qtile:
            if self.qtile.config.wl_input_rules:
                device.configure(self.qtile.config.wl_input_rules)
        else:
            self._pending_input_devices.append(device)

    def _on_new_output(self, _listener: Listener, wlr_output: wlrOutput) -> None:
        logger.debug("Signal: backend new_output_event")

        wlr_output.init_render(self._allocator, self.renderer)
        wlr_output.set_mode(wlr_output.preferred_mode())
        wlr_output.enable()
        wlr_output.commit()

        self.outputs.append(Output(self, wlr_output))
        # Put new output at far right
        layout_geo = self.output_layout.get_box()
        x = layout_geo.width if layout_geo else 0
        self.output_layout.add(wlr_output, x, 0)

        if not self._current_output:
            self._current_output = self.outputs[0]

    def _on_output_layout_change(self, _listener: Listener, _data: Any) -> None:
        logger.debug("Signal: output_layout change_event")
        config = OutputConfigurationV1()

        for output in self.outputs:
            head = OutputConfigurationHeadV1.create(config, output.wlr_output)
            mode = output.wlr_output.current_mode
            head.state.mode = mode
            head.state.enabled = mode is not None and output.wlr_output.enabled
            box = self.output_layout.get_box(output.wlr_output)
            head.state.x = output.x = box.x if box else 0
            head.state.y = output.y = box.y if box else 0

        self.output_manager.set_configuration(config)
        self.outputs.sort(key=lambda o: (o.x, o.y))

    def _on_output_manager_apply(
        self, _listener: Listener, config: OutputConfigurationV1
    ) -> None:
        logger.debug("Signal: output_manager apply_event")
        self._output_manager_reconfigure(config, True)

    def _on_output_manager_test(self, _listener: Listener, config: OutputConfigurationV1) -> None:
        logger.debug("Signal: output_manager test_event")
        self._output_manager_reconfigure(config, False)

    def _on_request_cursor(
        self, _listener: Listener, event: seat.PointerRequestSetCursorEvent
    ) -> None:
        if event._ptr.seat_client != self.seat.pointer_state._ptr.focused_client:
            # The request came from a cheeky window that doesn't have the pointer
            return

        self._cursor_state.surface = event.surface
        self._cursor_state.hotspot = event.hotspot

        if not self._cursor_state.hidden:
            self.cursor.set_surface(event.surface, event.hotspot)

    def _on_new_xdg_surface(self, _listener: Listener, surface: XdgSurface) -> None:
        logger.debug("Signal: xdg_shell new_surface_event")
        if surface.role == XdgSurfaceRole.TOPLEVEL:
            assert self.qtile is not None
            win = xdgwindow.XdgWindow(self, self.qtile, surface)
            self.pending_windows.add(win)

    def _on_cursor_axis(self, _listener: Listener, event: pointer.PointerEventAxis) -> None:
        handled = False

        if event.delta != 0 and not self.exclusive_client:
            # If we have a client who exclusively gets input, button bindings are disallowed.
            if event.orientation == pointer.AxisOrientation.VERTICAL:
                button = 5 if 0 < event.delta else 4
            else:
                button = 7 if 0 < event.delta else 6
            handled = self._process_cursor_button(button, True)

        if not handled:
            self.seat.pointer_notify_axis(
                event.time_msec,
                event.orientation,
                event.delta,
                event.delta_discrete,
                event.source,
            )

    def _on_cursor_frame(self, _listener: Listener, _data: Any) -> None:
        self.seat.pointer_notify_frame()

    def _on_cursor_button(self, _listener: Listener, event: pointer.PointerEventButton) -> None:
        assert self.qtile is not None
        self.idle.notify_activity(self.seat)
        pressed = event.button_state == input_device.ButtonState.PRESSED
        if pressed:
            self._focus_by_click()

        handled = False

        if not self.exclusive_client and event.button in wlrq.buttons:
            # If we have a client who exclusively gets input, button bindings are disallowed.
            button = wlrq.buttons.index(event.button) + 1
            handled = self._process_cursor_button(button, pressed)

        if not handled:
            self.seat.pointer_notify_button(event.time_msec, event.button, event.button_state)

    def _on_cursor_motion(self, _listener: Listener, event: pointer.PointerEventMotion) -> None:
        assert self.qtile is not None
        self.idle.notify_activity(self.seat)

        dx = event.delta_x
        dy = event.delta_y

        # Send relative pointer events to seat - used e.g. by games that have
        # constrained cursor movement but want movement events
        self._relative_pointer_manager_v1.send_relative_motion(
            self.seat,
            event.time_msec * 1000,
            dx,
            dy,
            event.unaccel_delta_x,
            event.unaccel_delta_y,
        )

        if self.active_pointer_constraint:
            if not self.active_pointer_constraint.rect.contains_point(
                self.cursor.x + dx, self.cursor.y + dy
            ):
                return

        self.cursor.move(dx, dy, input_device=event.device)
        self._process_cursor_motion(event.time_msec, self.cursor.x, self.cursor.y)

    def _on_cursor_motion_absolute(
        self, _listener: Listener, event: pointer.PointerEventMotionAbsolute
    ) -> None:
        assert self.qtile is not None
        self.idle.notify_activity(self.seat)
        self.cursor.warp(
            WarpMode.AbsoluteClosest,
            event.x,
            event.y,
            input_device=event.device,
        )
        self._process_cursor_motion(event.time_msec, self.cursor.x, self.cursor.y)

    def _on_cursor_pinch_begin(
        self,
        _listener: Listener,
        event: pointer.PointerEventPinchBegin,
    ) -> None:
        self.idle.notify_activity(self.seat)
        self._gestures.send_pinch_begin(self.seat, event.time_msec, event.fingers)

    def _on_cursor_pinch_update(
        self,
        _listener: Listener,
        event: pointer.PointerEventPinchUpdate,
    ) -> None:
        self._gestures.send_pinch_update(
            self.seat, event.time_msec, event.dx, event.dy, event.scale, event.rotation
        )

    def _on_cursor_pinch_end(
        self,
        _listener: Listener,
        event: pointer.PointerEventPinchEnd,
    ) -> None:
        self.idle.notify_activity(self.seat)
        self._gestures.send_pinch_end(self.seat, event.time_msec, event.cancelled)

    def _on_cursor_swipe_begin(
        self,
        _listener: Listener,
        event: pointer.PointerEventSwipeBegin,
    ) -> None:
        self.idle.notify_activity(self.seat)
        self._gestures.send_swipe_begin(self.seat, event.time_msec, event.fingers)

    def _on_cursor_swipe_update(
        self,
        _listener: Listener,
        event: pointer.PointerEventSwipeUpdate,
    ) -> None:
        self._gestures.send_swipe_update(self.seat, event.time_msec, event.dx, event.dy)

    def _on_cursor_swipe_end(
        self,
        _listener: Listener,
        event: pointer.PointerEventSwipeEnd,
    ) -> None:
        self.idle.notify_activity(self.seat)
        self._gestures.send_swipe_end(self.seat, event.time_msec, event.cancelled)

    def _on_cursor_hold_begin(
        self,
        _listener: Listener,
        event: pointer.PointerEventHoldBegin,
    ) -> None:
        self.idle.notify_activity(self.seat)
        self._gestures.send_hold_begin(self.seat, event.time_msec, event.fingers)

    def _on_cursor_hold_end(
        self,
        _listener: Listener,
        event: pointer.PointerEventHoldEnd,
    ) -> None:
        self.idle.notify_activity(self.seat)
        self._gestures.send_hold_end(self.seat, event.time_msec, event.cancelled)

    def _on_new_pointer_constraint(
        self, _listener: Listener, wlr_constraint: PointerConstraintV1
    ) -> None:
        logger.debug("Signal: pointer_constraints new_constraint")
        constraint = window.PointerConstraint(self, wlr_constraint)
        self.pointer_constraints.add(constraint)

        if self.seat.pointer_state.focused_surface == wlr_constraint.surface:
            if self.active_pointer_constraint:
                self.active_pointer_constraint.disable()
            constraint.enable()

    def _on_new_virtual_keyboard(
        self, _listener: Listener, virtual_keyboard: vkeyboard.VirtualKeyboardV1
    ) -> None:
        self._add_new_keyboard(virtual_keyboard.input_device)

    def _on_new_virtual_pointer(
        self, _listener: Listener, new_pointer_event: vpointer.VirtualPointerV1NewPointerEvent
    ) -> None:
        device = self._add_new_pointer(new_pointer_event.new_pointer.input_device)
        logger.info("New virtual pointer: %s %s", *device.get_info())

    def _on_new_idle_inhibitor(
        self, _listener: Listener, idle_inhibitor: IdleInhibitorV1
    ) -> None:
        logger.debug("Signal: idle_inhibitor new_inhibitor")

        if self.qtile is None:
            return

        for win in self.qtile.windows_map.values():
            if isinstance(win, (window.Window, window.Static)):
                win.surface.for_each_surface(win.add_idle_inhibitor, idle_inhibitor)
                if idle_inhibitor.data:
                    break

    def _on_input_inhibitor_activate(self, _listener: Listener, _data: Any) -> None:
        logger.debug("Signal: input_inhibitor activate")
        assert self.qtile is not None
        self.exclusive_client = self._input_inhibit_manager.active_client

        # If another client has keyboard focus, unfocus it.
        if self.qtile.current_window and not self.qtile.current_window.belongs_to_client(
            self.exclusive_client
        ):
            self.focus_window(None)

        # If another client has pointer focus, unfocus that too.
        found = self._under_pointer()
        if found:
            win, _, _, _ = found

            # If we have a client who exclusively gets input, no other client's
            # surfaces are allowed to get pointer input.
            if isinstance(win, base.Internal) or not win.belongs_to_client(self.exclusive_client):
                self.cursor_manager.set_cursor_image("left_ptr", self.cursor)
                self.seat.pointer_notify_clear_focus()
                return

    def _on_input_inhibitor_deactivate(self, _listener: Listener, _data: Any) -> None:
        logger.debug("Signal: input_inhibitor deactivate")
        self.exclusive_client = None

    def _on_output_power_manager_set_mode(
        self, _listener: Listener, mode: OutputPowerV1SetModeEvent
    ) -> None:
        """
        Blank/unblank outputs via the output power management protocol.

        `_blanked_outputs` keeps track of those that were blanked because we don't want
        to unblank outputs that were already disabled due to not being part of the
        user-configured layout.
        """
        logger.debug("Signal: output_power_manager set_mode_event")
        wlr_output = mode.output
        assert wlr_output.data

        if mode.mode == OutputPowerManagementV1Mode.ON:
            if wlr_output.data in self._blanked_outputs:
                wlr_output.enable(enable=True)
                wlr_output.commit()
                self._blanked_outputs.remove(wlr_output.data)

        else:
            if wlr_output.enabled:
                wlr_output.enable(enable=False)
                wlr_output.commit()
                self._blanked_outputs.add(wlr_output.data)

    def _on_new_layer_surface(self, _listener: Listener, layer_surface: LayerSurfaceV1) -> None:
        logger.debug("Signal: layer_shell new_surface_event")
        assert self.qtile is not None

        wid = self.new_wid()
        win = layer.LayerStatic(self, self.qtile, layer_surface, wid)
        logger.info("Managing new layer_shell window with window ID: %s", wid)
        self.qtile.manage(win)

    def _on_new_toplevel_decoration(
        self, _listener: Listener, decoration: xdg_decoration_v1.XdgToplevelDecorationV1
    ) -> None:
        logger.debug("Signal: xdg_decoration new_top_level_decoration")
        decoration.set_mode(xdg_decoration_v1.XdgToplevelDecorationV1Mode.SERVER_SIDE)

    def _on_xwayland_ready(self, _listener: Listener, _data: Any) -> None:
        logger.debug("Signal: xwayland ready")
        assert self._xwayland is not None
        self._xwayland.set_seat(self.seat)
        self.xwayland_atoms: dict[int, str] = wlrq.get_xwayland_atoms(self._xwayland)

        # Set the default XWayland cursor
        xcursor = self.cursor_manager.get_xcursor("left_ptr")
        if xcursor:
            image = next(xcursor.images, None)
            if image:
                self._xwayland.set_cursor(
                    image._ptr.buffer,
                    image._ptr.width * 4,
                    image._ptr.width,
                    image._ptr.height,
                    image._ptr.hotspot_x,
                    image._ptr.hotspot_y,
                )

    def _on_xwayland_new_surface(self, _listener: Listener, surface: xwayland.Surface) -> None:
        logger.debug("Signal: xwayland new_surface")
        assert self.qtile is not None
        win = xwindow.XWindow(self, self.qtile, surface)
        self.pending_windows.add(win)

    def _output_manager_reconfigure(self, config: OutputConfigurationV1, apply: bool) -> None:
        """
        See if an output configuration would be accepted by the backend, and apply it if
        desired.
        """
        ok = True

        for head in config.heads:
            state = head.state
            wlr_output = state.output

            if state.enabled:
                if not wlr_output.enabled:
                    wlr_output.enable()
                if state.mode:
                    wlr_output.set_mode(state.mode)
                else:
                    wlr_output.set_custom_mode(
                        state.custom_mode.width,
                        state.custom_mode.height,
                        state.custom_mode.refresh,
                    )

                # `add` will add outputs that have been removed. Any other outputs that
                # are already in the layout are just moved as if we had used `move`.
                self.output_layout.add(wlr_output, state.x, state.y)
                wlr_output.set_transform(state.transform)
                wlr_output.set_scale(state.scale)
            else:
                if wlr_output.enabled:
                    wlr_output.enable(enable=False)
                self.output_layout.remove(wlr_output)

            ok = wlr_output.test()
            if not ok:
                break

        for head in config.heads:
            if ok and apply:
                head.state.output.commit()
            else:
                head.state.output.rollback()

        if ok:
            config.send_succeeded()
        else:
            config.send_failed()
        config.destroy()
        hook.fire("screen_change", None)

    def _process_cursor_motion(self, time_msec: int, cx: float, cy: float) -> None:
        assert self.qtile
        cx_int = int(cx)
        cy_int = int(cy)

        if not self.exclusive_client:
            # If we have a client who exclusively gets input, button bindings are
            # disallowed, so process_button_motion doesn't need to be updated.
            self.qtile.process_button_motion(cx_int, cy_int)

        if len(self.outputs) > 1:
            current_wlr_output = self.output_layout.output_at(cx, cy)
            if current_wlr_output:
                current_output = current_wlr_output.data
                if self._current_output is not current_output:
                    self._current_output = current_output
                    self.stack_windows()

        if self.live_dnd:
            self.live_dnd.position(cx, cy)

        self._focus_pointer(cx_int, cy_int, motion=time_msec)

    def _focus_pointer(self, cx: int, cy: int, motion: int | None = None) -> None:
        assert self.qtile is not None
        found = self._under_pointer()

        if found:
            win, surface, sx, sy = found

            if self.exclusive_client:
                # If we have a client who exclusively gets input, no other client's
                # surfaces are allowed to get pointer input.
                if isinstance(win, base.Internal) or not win.belongs_to_client(
                    self.exclusive_client
                ):
                    # Moved to an internal or unrelated window
                    if self._hovered_window is not win:
                        logger.debug(
                            "Pointer focus withheld from window not owned by exclusive client."
                        )
                        self.cursor_manager.set_cursor_image("left_ptr", self.cursor)
                        self.seat.pointer_notify_clear_focus()
                        self._hovered_window = win
                    return

            if isinstance(win, window.Internal):
                if self._hovered_window is win:
                    # pointer remained within the same Internal window
                    if motion is not None:
                        win.process_pointer_motion(
                            cx - self._hovered_window.x,
                            cy - self._hovered_window.y,
                        )
                else:
                    if self._hovered_window:
                        if isinstance(self._hovered_window, window.Internal):
                            if motion is not None:
                                # moved from an Internal to a different Internal
                                self._hovered_window.process_pointer_leave(
                                    cx - self._hovered_window.x,
                                    cy - self._hovered_window.y,
                                )
                        elif self.seat.pointer_state.focused_surface:
                            # moved from a Window or Static to an Internal
                            self.cursor_manager.set_cursor_image("left_ptr", self.cursor)
                            self.seat.pointer_notify_clear_focus()
                    win.process_pointer_enter(cx, cy)
                    self._hovered_window = win
                return

            if surface:
                # The pointer is in a client's surface
                self.seat.pointer_notify_enter(surface, sx, sy)
                if motion is not None:
                    self.seat.pointer_notify_motion(motion, sx, sy)
            else:
                # The pointer is on the border of a client's window
                if self.seat.pointer_state.focused_surface:
                    # We just moved out of a client's surface
                    self.cursor_manager.set_cursor_image("left_ptr", self.cursor)
                    self.seat.pointer_notify_clear_focus()

            if win is not self.qtile.current_window:
                if isinstance(win, window.Static):
                    if self._hovered_window is not win:
                        # qtile.current_window will never be a static window, but we
                        # still only want to fire client_mouse_enter once, so check
                        # self._hovered_window.
                        hook.fire("client_mouse_enter", win)

                    if motion is not None and self.qtile.config.follow_mouse_focus:
                        self.qtile.focus_screen(win.screen.index, False)

                else:
                    hook.fire("client_mouse_enter", win)

                    if motion is not None and self.qtile.config.follow_mouse_focus:
                        if win.group and win.group.current_window != win:
                            win.group.focus(win, False)
                        if (
                            win.group
                            and win.group.screen
                            and self.qtile.current_screen != win.group.screen
                        ):
                            self.qtile.focus_screen(win.group.screen.index, False)

            self._hovered_window = win

        else:
            # There is no window under the pointer
            if self._hovered_window:
                if isinstance(self._hovered_window, window.Internal):
                    # We just moved out of an Internal
                    self._hovered_window.process_pointer_leave(
                        cx - self._hovered_window.x,
                        cy - self._hovered_window.y,
                    )
                else:
                    # We just moved out of a Window or Static
                    self.cursor_manager.set_cursor_image("left_ptr", self.cursor)
                    self.seat.pointer_notify_clear_focus()
                self._hovered_window = None

    def _process_cursor_button(self, button: int, pressed: bool) -> bool:
        assert self.qtile is not None

        if pressed:
            handled = self.qtile.process_button_click(
                button, self.seat.keyboard.modifier, int(self.cursor.x), int(self.cursor.y)
            )

            if isinstance(self._hovered_window, window.Internal):
                self._hovered_window.process_button_click(
                    int(self.cursor.x - self._hovered_window.x),
                    int(self.cursor.y - self._hovered_window.y),
                    button,
                )
        else:
            handled = self.qtile.process_button_release(button, self.seat.keyboard.modifier)

            if isinstance(self._hovered_window, window.Internal):
                self._hovered_window.process_button_release(
                    int(self.cursor.x - self._hovered_window.x),
                    int(self.cursor.y - self._hovered_window.y),
                    button,
                )

        return handled

    def _add_new_pointer(self, wlr_device: input_device.InputDevice) -> inputs.Pointer:
        device = inputs.Pointer(self, wlr_device)
        self._pointers.append(device)
        self.cursor.attach_input_device(wlr_device)
        self.cursor_manager.set_cursor_image("left_ptr", self.cursor)
        return device

    def _add_new_keyboard(self, wlr_device: input_device.InputDevice) -> inputs.Keyboard:
        device = inputs.Keyboard(self, wlr_device)
        self.keyboards.append(device)
        self.seat.set_keyboard(wlr_device)
        return device

    def _configure_pending_inputs(self) -> None:
        """Configure inputs that were detected before the config was loaded."""
        assert self.qtile is not None

        if self.qtile.config.wl_input_rules:
            for device in self._pending_input_devices:
                device.configure(self.qtile.config.wl_input_rules)
        self._pending_input_devices.clear()

    def setup_listener(self, qtile: Qtile) -> None:
        """Setup a listener for the given qtile instance"""
        logger.debug("Adding io watch")
        self.qtile = qtile
        self.fd = wllib.wl_event_loop_get_fd(self.event_loop._ptr)
        if self.fd:
            asyncio.get_running_loop().add_reader(self.fd, self._poll)
        else:
            raise RuntimeError("Failed to get Wayland event loop file descriptor.")

    def remove_listener(self) -> None:
        """Remove the listener from the given event loop"""
        if self.fd is not None:
            logger.debug("Removing io watch")
            loop = asyncio.get_running_loop()
            loop.remove_reader(self.fd)
            self.fd = None

    def _poll(self) -> None:
        if not self.display.destroyed:
            self.display.flush_clients()
            self.event_loop.dispatch(0)
            self.display.flush_clients()

    def on_config_load(self, initial: bool) -> None:
        if initial:
            # This backend does not support restarting
            return

        assert self.qtile is not None

        for win in self.qtile.windows_map.values():
            if not isinstance(win, window.Window):
                continue

            group = None
            if win.group:
                if win.group.name in self.qtile.groups_map:
                    # Put window on group with same name as its old group if one exists
                    group = self.qtile.groups_map[win.group.name]
                else:
                    # Otherwise place it on the group at the same index
                    for i, old_group in self.qtile._state.groups:  # type: ignore
                        if i < len(self.qtile.groups):
                            name = old_group[0]
                            if win.group.name == name:
                                group = self.qtile.groups[i]
            if group is None:
                # Falling back to current group if none found
                group = self.qtile.current_group
            if win.group and win in win.group.windows:
                # It might not be in win.group.windows depending on how group state
                # changed across a config reload
                win.group.remove(win)
            group.add(win)
            if group == self.qtile.current_group:
                win.unhide()
            else:
                win.hide()

        # Apply input device configuration
        if self.qtile.config.wl_input_rules:
            for device in [*self.keyboards, *self._pointers]:
                device.configure(self.qtile.config.wl_input_rules)

    def new_wid(self) -> int:
        """Get a new unique window ID"""
        assert self.qtile is not None
        return max(self.qtile.windows_map.keys(), default=0) + 1

    def focus_window(
        self, win: window.WindowType | None, surface: Surface | None = None, enter: bool = True
    ) -> None:
        if self.seat.destroyed:
            return

        if self.exclusive_client:
            # If we have a client who exclusively gets input, no other client's surfaces
            # are allowed to get keyboard input.
            if not win:
                self.seat.keyboard_clear_focus()
                return
            if isinstance(win, base.Internal) or not win.belongs_to_client(self.exclusive_client):
                logger.debug("Keyboard focus withheld from window not owned by exclusive client.")
                # We can't focus surfaces belonging to other clients.
                return

        if isinstance(win, base.Internal):
            self.focused_internal = win
            self.seat.keyboard_clear_focus()
            return

        if surface is None and win is not None:
            surface = win.surface.surface

        if self.focused_internal:
            self.focused_internal = None

        if isinstance(win, layer.LayerStatic):
            if not win.surface.current.keyboard_interactive:
                return

        if isinstance(win, xwindow.XStatic):
            if win.surface.override_redirect and not win.surface.or_surface_wants_focus():
                return
            if win.surface.icccm_input_model() == xwayland.ICCCMInputModel.NONE:
                return

        previous_surface = self.seat.keyboard_state.focused_surface
        if previous_surface == surface:
            return

        if previous_surface is not None:
            # Deactivate the previously focused surface
            if previous_surface.is_xdg_surface:
                previous_xdg_surface = XdgSurface.from_surface(previous_surface)
                if not win or win.surface != previous_xdg_surface:
                    previous_xdg_surface.set_activated(False)
                    if previous_xdg_surface.data:
                        previous_xdg_surface.data.set_activated(False)

            elif previous_surface.is_xwayland_surface:
                prev_xwayland_surface = xwayland.Surface.from_wlr_surface(previous_surface)
                if not win or win.surface != prev_xwayland_surface:
                    prev_xwayland_surface.activate(False)
                    if prev_xwayland_surface.data:
                        prev_xwayland_surface.data.set_activated(False)

        if not win or not surface:
            self.seat.keyboard_clear_focus()
            return

        logger.debug("Focusing new window")
        if surface.is_xdg_surface and isinstance(win.surface, XdgSurface):
            win.surface.set_activated(True)
            if win.ftm_handle:
                win.ftm_handle.set_activated(True)

        elif surface.is_xwayland_surface and isinstance(win.surface, xwayland.Surface):
            win.surface.activate(True)
            if win.ftm_handle:
                win.ftm_handle.set_activated(True)

        if enter and self.seat.keyboard._ptr:  # This pointer is NULL when headless
            self.seat.keyboard_notify_enter(surface, self.seat.keyboard)

    def _focus_by_click(self) -> None:
        assert self.qtile is not None
        found = self._under_pointer()

        if found:
            win, _, _, _ = found

            if self.exclusive_client:
                # If we have a client who exclusively gets input, no other client's
                # surfaces are allowed to get focus.
                if isinstance(win, base.Internal) or not win.belongs_to_client(
                    self.exclusive_client
                ):
                    logger.debug("Focus withheld from window not owned by exclusive client.")
                    return

            if self.qtile.config.bring_front_click is True:
                win.bring_to_front()
            elif self.qtile.config.bring_front_click == "floating_only":
                if isinstance(win, base.Window) and win.floating:
                    win.bring_to_front()
                elif isinstance(win, base.Static):
                    win.bring_to_front()

            if isinstance(win, window.Static):
                if win.screen is not self.qtile.current_screen:
                    self.qtile.focus_screen(win.screen.index, warp=False)
                win.focus(False)
            elif isinstance(win, window.Window):
                if win.group and win.group.screen is not self.qtile.current_screen:
                    self.qtile.focus_screen(win.group.screen.index, warp=False)
                self.qtile.current_group.focus(win, False)

        else:
            screen = self.qtile.find_screen(int(self.cursor.x), int(self.cursor.y))
            if screen:
                self.qtile.focus_screen(screen.index, warp=False)

    def _under_pointer(self) -> tuple[window.WindowType, Surface | None, float, float] | None:
        assert self.qtile is not None

        cx = self.cursor.x
        cy = self.cursor.y

        for win in reversed(self.stacked_windows):
            if isinstance(win, window.Internal):
                if win.x <= cx <= win.x + win.width and win.y <= cy <= win.y + win.height:
                    return win, None, 0, 0
            else:
                bw = win.borderwidth
                surface, sx, sy = win.surface.surface_at(cx - win.x - bw, cy - win.y - bw)
                if surface:
                    return win, surface, sx, sy
                if bw:
                    if win.x <= cx and win.y <= cy:
                        bw *= 2
                        if cx <= win.x + win.width + bw and cy <= win.y + win.height + bw:
                            return win, None, 0, 0
        return None

    def stack_windows(self, restack: window.WindowType | None = None) -> None:
        """
        Put all windows of all types in a Z-ordered list.

        A (non-layer_shell) window passed as 'restack' will bubble up the Z order.
        """
        if restack:
            self.mapped_windows.remove(restack)
            self.mapped_windows.append(restack)

        if self._current_output:
            layers = self._current_output.layers
            self.stacked_windows = (
                layers[LayerShellV1Layer.BACKGROUND]
                + layers[LayerShellV1Layer.BOTTOM]
                + self.mapped_windows  # type: ignore
                + layers[LayerShellV1Layer.TOP]
                + layers[LayerShellV1Layer.OVERLAY]
            )
        else:
            self.stacked_windows = self.mapped_windows.copy()

    def check_idle_inhibitor(self) -> None:
        """
        Checks if any window that is currently mapped has idle inhibitor
        and if so inhibits idle
        """
        for win in self.mapped_windows:
            if isinstance(win, (window.Window, window.Static)) and win.is_idle_inhibited:
                self.idle.set_enabled(self.seat, False)
                break
        else:
            self.idle.set_enabled(self.seat, True)

    def get_screen_info(self) -> list[tuple[int, int, int, int]]:
        """Get the screen information"""
        return [screen.get_geometry() for screen in self.outputs if screen.wlr_output.enabled]

    def grab_key(self, key: config.Key | config.KeyChord) -> tuple[int, int]:
        """Configure the backend to grab the key event"""
        keysym = xkb.keysym_from_name(key.key, case_insensitive=True)
        mask_key = wlrq.translate_masks(key.modifiers)
        self.grabbed_keys.append((keysym, mask_key))
        return keysym, mask_key

    def ungrab_key(self, key: config.Key | config.KeyChord) -> tuple[int, int]:
        """Release the given key event"""
        keysym = xkb.keysym_from_name(key.key, case_insensitive=True)
        mask_key = wlrq.translate_masks(key.modifiers)
        self.grabbed_keys.remove((keysym, mask_key))
        return keysym, mask_key

    def ungrab_keys(self) -> None:
        """Release the grabbed key events"""
        self.grabbed_keys.clear()

    def grab_button(self, mouse: config.Mouse) -> int:
        """Configure the backend to grab the mouse event"""
        return wlrq.translate_masks(mouse.modifiers)

    def warp_pointer(self, x: float, y: float) -> None:
        """Warp the pointer to the coordinates in relative to the output layout"""
        self.cursor.warp(WarpMode.LayoutClosest, x, y)

    @contextlib.contextmanager
    def masked(self) -> Generator:
        yield
        self._focus_pointer(int(self.cursor.x), int(self.cursor.y))

    def flush(self) -> None:
        self._poll()

    def create_internal(self, x: int, y: int, width: int, height: int) -> base.Internal:
        assert self.qtile is not None
        internal = window.Internal(self, self.qtile, x, y, width, height)
        self.qtile.manage(internal)
        return internal

    def graceful_shutdown(self) -> None:
        """Try to close windows gracefully before exiting"""
        assert self.qtile is not None

        # Copy in case the dictionary changes during the loop
        for win in self.qtile.windows_map.copy().values():
            win.kill()

        # give everyone a little time to exit and write their state. but don't
        # sleep forever (1s).
        end = time.time() + 1
        while time.time() < end:
            self._poll()
            if not self.qtile.windows_map:
                break

    @property
    def painter(self) -> Any:
        return wlrq.Painter(self)

    def remove_output(self, output: Output) -> None:
        self.outputs.remove(output)
        self.output_layout.remove(output.wlr_output)
        if output is self._current_output:
            self._current_output = self.outputs[0] if self.outputs else None
            self.stack_windows()

    def remove_pointer_constraints(self, window: window.Window | window.Static) -> None:
        for pc in self.pointer_constraints.copy():
            if pc.window is window:
                pc.finalize()

    def keysym_from_name(self, name: str) -> int:
        """Get the keysym for a key from its name"""
        return xkb.keysym_from_name(name, case_insensitive=True)

    def simulate_keypress(self, modifiers: list[str], key: str) -> None:
        """Simulates a keypress on the focused window."""
        keysym = xkb.keysym_from_name(key, case_insensitive=True)
        mods = wlrq.translate_masks(modifiers)

        if (keysym, mods) in self.grabbed_keys:
            assert self.qtile is not None
            self.qtile.process_key_event(keysym, mods)
            return

        if self.focused_internal:
            self.focused_internal.process_key_press(keysym)

    @expose_command()
    def set_keymap(
        self,
        layout: str | None = None,
        options: str | None = None,
        variant: str | None = None,
    ) -> None:
        """
        Set the keymap for the current keyboard.

        The options correspond to xkbcommon configuration environmental variables and if
        not specified are taken from the environment. Acceptable values are strings
        identical to those accepted by the env variables.
        """
        if self.keyboards:
            for keyboard in self.keyboards:
                keyboard.set_keymap(layout, options, variant)
        else:
            logger.warning("Could not set keymap: no keyboards set up.")

    @expose_command()
    def change_vt(self, vt: int) -> bool:
        """Change virtual terminal to that specified"""
        success = self.backend.get_session().change_vt(vt)
        if not success:
            logger.warning("Could not change VT to: %s", vt)
        return success

    @expose_command()
    def hide_cursor(self) -> None:
        """Hide the cursor."""
        if not self._cursor_state.hidden:
            self.cursor.set_surface(None, self._cursor_state.hotspot)
            self._cursor_state.hidden = True

    @expose_command()
    def unhide_cursor(self) -> None:
        """Unhide the cursor."""
        if self._cursor_state.hidden:
            self.cursor.set_surface(
                self._cursor_state.surface,
                self._cursor_state.hotspot,
            )
            self._cursor_state.hidden = False

    @expose_command()
    def get_inputs(self) -> dict[str, list[dict[str, str]]]:
        """Get information on all input devices."""
        info: defaultdict[str, list[dict]] = defaultdict(list)
        devices: list[inputs._Device] = self.keyboards + self._pointers  # type: ignore

        for dev in devices:
            type_key, identifier = dev.get_info()
            type_info = dict(
                name=dev.wlr_device.name,
                identifier=identifier,
            )
            info[type_key].append(type_info)

        return dict(info)

    def get_mouse_position(self) -> tuple[int, int]:
        """Get mouse coordinates."""
        return int(self.cursor.x), int(self.cursor.y)
