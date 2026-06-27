#!/usr/bin/env python3
"""
IOTA Terminal Test Station App

Terminal UI for Smart Tubing thermal-flow prototyping.

Features:
- Arduino serial dashboard for thermal-flow firmware
- Optional Alicat MFC polling panel
- CSV logging
- Raw Arduino command entry
- Keyboard shortcuts that work even when command input is focused
- Demo mode without hardware

Examples:
    python iota_term_app.py --demo

    python iota_term_app.py \
        --arduino-port /dev/ttyACM0 \
        --arduino-baud 115200

    python iota_term_app.py \
        --arduino-port /dev/ttyACM0 \
        --arduino-baud 115200 \
        --mfc-port /dev/ttyUSB0 \
        --mfc-baud 38400
"""

from __future__ import annotations

import argparse
import csv
import math
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import serial

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid
from textual.widgets import Footer, Header, Input, Static


# =============================================================================
# Data models
# =============================================================================


@dataclass
class ArduinoTelemetry:
    valid: bool = False
    raw_line: str = ""
    columns: int = 0
    last_update_time: float = 0.0

    millis: float = math.nan
    t_s: float = math.nan

    ref_C: float = math.nan
    heater_C: float = math.nan
    ambient_C: float = math.nan
    humidity_pct: float = math.nan

    bus_V: float = math.nan
    shunt_mV: float = math.nan
    load_V: float = math.nan
    current_mA: float = math.nan

    target_C: float = math.nan
    deltaT_C: float = math.nan
    pwm: float = math.nan
    power_W: float = math.nan
    power_avg_W: float = math.nan
    power_filt_W: float = math.nan

    pid_enabled: int = 0
    ref_mode: int = 0
    baseline_ready: int = 0
    baseline_capture_active: int = 0
    alarm: int = 0
    el_state: int = 0

    baseline_power_W: float = math.nan
    power_change_percent: float = math.nan
    trigger_level_W: float = math.nan
    clear_level_W: float = math.nan

    parse_error: str = ""


@dataclass
class MfcTelemetry:
    valid: bool = False
    connected: bool = False
    raw_line: str = ""
    last_update_time: float = 0.0
    parse_error: str = ""

    unit_id: str = "A"
    pressure: float = math.nan
    temperature_C: float = math.nan
    volumetric_flow: float = math.nan
    mass_flow: float = math.nan
    setpoint: float = math.nan
    gas: str = ""


# =============================================================================
# Parsing helpers
# =============================================================================


def safe_int(value: float) -> int:
    if not math.isfinite(value):
        return 0
    return int(round(value))


def fmt(value: float, unit: str = "", digits: int = 2) -> str:
    if not math.isfinite(value):
        return f"-- {unit}".rstrip()
    return f"{value:.{digits}f} {unit}".rstrip()


def format_csv_value(value: float | int) -> str:
    if isinstance(value, float) and math.isnan(value):
        return "nan"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def parse_arduino_line(line: str) -> ArduinoTelemetry:
    """
    Parse current Arduino CSV line.

    Current firmware prints 25 columns, but we accept >=20 to stay robust
    if extra fields are added later.
    """

    tel = ArduinoTelemetry(raw_line=line, last_update_time=time.time())

    parts = line.strip().split(",")
    tel.columns = len(parts)

    if len(parts) < 20:
        tel.parse_error = f"Too few columns: {len(parts)}"
        return tel

    try:
        values = [float(x) for x in parts]
    except ValueError as exc:
        tel.parse_error = f"Float parse error: {exc}"
        return tel

    try:
        tel.millis = values[0]
        tel.t_s = values[0] / 1000.0

        tel.ref_C = values[1]
        tel.heater_C = values[2]
        tel.ambient_C = values[3]

        tel.humidity_pct = values[4]
        tel.bus_V = values[5]
        tel.shunt_mV = values[6]
        tel.load_V = values[7]
        tel.current_mA = values[8]

        tel.target_C = values[9]
        tel.deltaT_C = values[10]
        tel.pwm = values[11]
        tel.power_W = values[12]
        tel.power_avg_W = values[13]
        tel.power_filt_W = values[14]

        tel.pid_enabled = safe_int(values[15])
        tel.ref_mode = safe_int(values[16])
        tel.baseline_ready = safe_int(values[17])
        tel.baseline_capture_active = safe_int(values[18])
        tel.alarm = safe_int(values[19])

        if len(values) > 20:
            tel.el_state = safe_int(values[20])
        if len(values) > 21:
            tel.baseline_power_W = values[21]
        if len(values) > 22:
            tel.power_change_percent = values[22]
        if len(values) > 23:
            tel.trigger_level_W = values[23]
        if len(values) > 24:
            tel.clear_level_W = values[24]

    except Exception as exc:
        tel.parse_error = f"Mapping error: {exc}"
        return tel

    required = [
        tel.millis,
        tel.ref_C,
        tel.heater_C,
        tel.target_C,
        tel.deltaT_C,
        tel.pwm,
        tel.power_W,
        tel.power_avg_W,
    ]

    if not all(math.isfinite(x) for x in required):
        tel.parse_error = "Core field contains nan/inf"
        return tel

    tel.valid = True
    return tel


def parse_mfc_line(line: str) -> MfcTelemetry:
    """
    Parse Alicat-style MFC response.

    Observed response:
        A +22.4 +00.00 +000000.00 +00.00 +000.00 Air

    Likely fields:
        unit_id pressure temperature volumetric_flow mass_flow setpoint gas

    We keep labels somewhat generic until confirmed under flow.
    """

    tel = MfcTelemetry(raw_line=line, last_update_time=time.time(), connected=True)

    parts = line.strip().split()

    if len(parts) < 7:
        tel.parse_error = f"Too few MFC fields: {len(parts)}"
        return tel

    try:
        tel.unit_id = parts[0]
        tel.pressure = float(parts[1])
        tel.temperature_C = float(parts[2])
        tel.volumetric_flow = float(parts[3])
        tel.mass_flow = float(parts[4])
        tel.setpoint = float(parts[5])
        tel.gas = " ".join(parts[6:])
    except Exception as exc:
        tel.parse_error = f"MFC parse error: {exc}"
        return tel

    tel.valid = True
    return tel


# =============================================================================
# Serial workers
# =============================================================================


class ArduinoWorker:
    def __init__(
        self,
        port: Optional[str],
        baud: int,
        demo: bool,
        on_line: Callable[[str], None],
        on_status: Callable[[str], None],
    ):
        self.port = port
        self.baud = baud
        self.demo = demo
        self.on_line = on_line
        self.on_status = on_status

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._serial_lock = threading.Lock()
        self._ser: Optional[serial.Serial] = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._serial_lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:
                    pass
                self._ser = None

    def send_command(self, command: str) -> None:
        command = command.strip()
        if not command:
            return

        if self.demo:
            self.on_status(f"DEMO Arduino command: {command}")
            return

        with self._serial_lock:
            if self._ser is None:
                self.on_status("Arduino command failed: serial port not open")
                return

            try:
                self._ser.write((command + "\n").encode("utf-8"))
                self._ser.flush()
                self.on_status(f"Sent Arduino command: {command}")
            except Exception as exc:
                self.on_status(f"Arduino write error: {exc}")

    def _run(self) -> None:
        if self.demo:
            self._run_demo()
        else:
            self._run_serial()

    def _run_demo(self) -> None:
        self.on_status("Arduino demo mode running")
        t0 = time.time()

        while not self._stop.is_set():
            elapsed = time.time() - t0

            ref = 23.0 + 0.1 * math.sin(elapsed / 20.0)
            heater = ref + 10.0 + 0.2 * math.sin(elapsed / 5.0)
            target = ref + 10.0
            delta_t = heater - ref
            pwm = 55.0 + 5.0 * math.sin(elapsed / 8.0)
            power = 0.28 + 0.02 * math.sin(elapsed / 12.0) + random.uniform(-0.002, 0.002)
            alarm = 1 if int(elapsed) % 60 > 45 else 0

            values = [
                elapsed * 1000.0,
                ref,
                heater,
                22.8,
                math.nan,
                math.nan,
                math.nan,
                math.nan,
                math.nan,
                target,
                delta_t,
                pwm,
                power,
                power,
                power,
                1,
                1,
                1,
                0,
                alarm,
                1,
                0.275,
                100.0 * (power - 0.275) / 0.275,
                0.255,
                0.267,
            ]

            line = ",".join(format_csv_value(v) for v in values)
            self.on_line(line)
            time.sleep(0.5)

    def _run_serial(self) -> None:
        if not self.port:
            self.on_status("Arduino disabled: no --arduino-port specified")
            return

        try:
            ser = serial.Serial(self.port, self.baud, timeout=1.0)
            time.sleep(2.0)  # Arduino often resets when serial opens.
            with self._serial_lock:
                self._ser = ser
            self.on_status(f"Arduino connected: {self.port} at {self.baud} baud")
        except Exception as exc:
            self.on_status(f"Arduino open error: {exc}")
            return

        while not self._stop.is_set():
            try:
                with self._serial_lock:
                    ser = self._ser

                if ser is None:
                    break

                raw = ser.readline()
                if not raw:
                    continue

                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    self.on_line(line)

            except Exception as exc:
                self.on_status(f"Arduino read error: {exc}")
                time.sleep(0.5)


class MfcWorker:
    def __init__(
        self,
        port: Optional[str],
        baud: int,
        unit_id: str,
        demo: bool,
        on_line: Callable[[str], None],
        on_status: Callable[[str], None],
    ):
        self.port = port
        self.baud = baud
        self.unit_id = unit_id
        self.demo = demo
        self.on_line = on_line
        self.on_status = on_status

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._serial_lock = threading.Lock()
        self._ser: Optional[serial.Serial] = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._serial_lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:
                    pass
                self._ser = None

    def set_flow(self, flow_slpm: float) -> None:
        """
        Placeholder for MFC setpoint command.

        For Alicat, the likely command is AS1.00 for unit A, 1.00 SLPM.
        We will confirm after plumbing is connected.
        """
        command = f"{self.unit_id}S{flow_slpm:.2f}"
        self.send_raw_command(command)

    def send_raw_command(self, command: str) -> None:
        command = command.strip()
        if not command:
            return

        if self.demo:
            self.on_status(f"DEMO MFC command: {command}")
            return

        with self._serial_lock:
            if self._ser is None:
                self.on_status("MFC command failed: serial port not open")
                return

            try:
                self._ser.write((command + "\r\n").encode("ascii"))
                self._ser.flush()
                self.on_status(f"Sent MFC command: {command}")
            except Exception as exc:
                self.on_status(f"MFC write error: {exc}")

    def _run(self) -> None:
        if self.demo:
            self._run_demo()
        else:
            self._run_serial()

    def _run_demo(self) -> None:
        self.on_status("MFC demo mode running")
        t0 = time.time()

        while not self._stop.is_set():
            elapsed = time.time() - t0
            flow = 2.0 + 1.0 * math.sin(elapsed / 10.0)
            setpoint = 2.0
            pressure = 22.4 + 0.1 * math.sin(elapsed / 15.0)
            temp = 23.0 + 0.2 * math.sin(elapsed / 30.0)

            line = f"A {pressure:+05.1f} {temp:+06.2f} {flow:+09.2f} {flow:+06.2f} {setpoint:+07.2f} Air"
            self.on_line(line)
            time.sleep(0.75)

    def _run_serial(self) -> None:
        if not self.port:
            self.on_status("MFC disabled: no --mfc-port specified")
            return

        try:
            ser = serial.Serial(
                self.port,
                baudrate=self.baud,
                bytesize=8,
                parity=serial.PARITY_NONE,
                stopbits=1,
                timeout=1.0,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )
            ser.setRTS(False)
            ser.setDTR(False)
            time.sleep(0.5)

            with self._serial_lock:
                self._ser = ser

            self.on_status(f"MFC connected: {self.port} at {self.baud} baud")
        except Exception as exc:
            self.on_status(f"MFC open error: {exc}")
            return

        while not self._stop.is_set():
            try:
                with self._serial_lock:
                    ser = self._ser

                if ser is None:
                    break

                ser.reset_input_buffer()
                poll = (self.unit_id + "\r\n").encode("ascii")
                ser.write(poll)
                ser.flush()

                time.sleep(0.2)
                raw = ser.read(500)
                line = raw.decode("ascii", errors="replace").strip()

                if line:
                    self.on_line(line)

                time.sleep(0.5)

            except Exception as exc:
                self.on_status(f"MFC read error: {exc}")
                time.sleep(1.0)


# =============================================================================
# Textual app
# =============================================================================


class IotaTermApp(App):
    CSS = """
    Screen {
        layout: vertical;
    }

    #main_grid {
        grid-size: 2;
        grid-columns: 1fr 1fr;
        grid-rows: auto auto auto auto;
        padding: 1;
    }

    .panel {
        border: solid $primary;
        padding: 1;
        height: auto;
    }

    #status {
        border: solid $secondary;
        padding: 1;
        margin: 0 1;
    }

    #command_box {
        margin: 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", priority=True),
        Binding("l", "toggle_logging", "Log", priority=True),
        Binding("r", "reset_start_time", "Reset time", priority=True),
        Binding("p", "send_arduino_pid_toggle", "PID toggle", priority=True),
        Binding("b", "send_arduino_capture_baseline", "Baseline", priority=True),
        Binding("c", "send_arduino_clear_baseline", "Clear base", priority=True),
        Binding("m", "mfc_setpoint_prompt", "MFC set", priority=True),
        Binding("ctrl+c", "quit", "Quit", priority=True),
    ]

    def __init__(
        self,
        arduino_port: Optional[str],
        arduino_baud: int,
        mfc_port: Optional[str],
        mfc_baud: int,
        mfc_unit_id: str,
        demo: bool,
        log_dir: Path,
    ):
        super().__init__()

        self.arduino_port = arduino_port
        self.arduino_baud = arduino_baud
        self.mfc_port = mfc_port
        self.mfc_baud = mfc_baud
        self.mfc_unit_id = mfc_unit_id
        self.demo = demo
        self.log_dir = log_dir

        self.arduino = ArduinoTelemetry()
        self.mfc = MfcTelemetry(unit_id=mfc_unit_id)

        self.arduino_lock = threading.Lock()
        self.mfc_lock = threading.Lock()

        self.status_message = "Starting..."
        self.arduino_line_count = 0
        self.arduino_valid_count = 0
        self.arduino_error_count = 0
        self.mfc_line_count = 0
        self.mfc_valid_count = 0
        self.mfc_error_count = 0

        self.logging_enabled = False
        self.log_file = None
        self.csv_writer = None
        self.log_path: Optional[Path] = None

        self.t0: Optional[float] = None

        self.arduino_worker = ArduinoWorker(
            port=arduino_port,
            baud=arduino_baud,
            demo=demo,
            on_line=self.handle_arduino_line,
            on_status=self.handle_status,
        )

        self.mfc_worker = MfcWorker(
            port=mfc_port,
            baud=mfc_baud,
            unit_id=mfc_unit_id,
            demo=demo,
            on_line=self.handle_mfc_line,
            on_status=self.handle_status,
        )

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        with Grid(id="main_grid"):
            yield Static("Waiting for Arduino data...", id="temperatures", classes="panel")
            yield Static("Waiting for Arduino data...", id="power", classes="panel")
            yield Static("Waiting for Arduino status...", id="status_panel", classes="panel")
            yield Static("Waiting for detection data...", id="detection", classes="panel")
            yield Static("Waiting for serial data...", id="serial_info", classes="panel")
            yield Static("Waiting for MFC data...", id="mfc_panel", classes="panel")
            yield Static("Controls...", id="help_panel", classes="panel")
            yield Static("Notes...", id="notes_panel", classes="panel")

        yield Static("Status: starting...", id="status")
        yield Input(
            placeholder=(
                "Raw command. Prefix with mfc: for MFC. "
                "Example: Arduino command or mfc:AS1.00"
            ),
            id="command_box",
        )
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(0.25, self.refresh_dashboard)
        self.arduino_worker.start()
        self.mfc_worker.start()
        self.query_one("#command_box", Input).focus()

    def on_unmount(self) -> None:
        self.arduino_worker.stop()
        self.mfc_worker.stop()
        self.close_log()

    def handle_status(self, message: str) -> None:
        self.status_message = message

    def handle_arduino_line(self, line: str) -> None:
        tel = parse_arduino_line(line)

        with self.arduino_lock:
            self.arduino = tel

        self.arduino_line_count += 1

        if tel.valid:
            self.arduino_valid_count += 1
            if self.t0 is None:
                self.t0 = tel.t_s
            if self.logging_enabled:
                self.write_log_row()
        else:
            self.arduino_error_count += 1

    def handle_mfc_line(self, line: str) -> None:
        tel = parse_mfc_line(line)

        with self.mfc_lock:
            self.mfc = tel

        self.mfc_line_count += 1

        if tel.valid:
            self.mfc_valid_count += 1
            if self.logging_enabled:
                self.write_log_row()
        else:
            self.mfc_error_count += 1

    def refresh_dashboard(self) -> None:
        with self.arduino_lock:
            a = self.arduino
        with self.mfc_lock:
            m = self.mfc

        now = time.time()
        arduino_age_s = now - a.last_update_time if a.last_update_time else math.nan
        mfc_age_s = now - m.last_update_time if m.last_update_time else math.nan

        alarm_text = "[white on red bold]BLOCKAGE ALARM[/]" if a.alarm else "[green bold]OK[/]"
        pid_text = "[green bold]ON[/]" if a.pid_enabled else "[yellow]OFF[/]"
        baseline_text = "[green bold]READY[/]" if a.baseline_ready else "[yellow]NOT READY[/]"
        capture_text = "[yellow bold]CAPTURING[/]" if a.baseline_capture_active else "idle"
        logging_text = f"[green bold]ON[/] {self.log_path}" if self.logging_enabled else "[yellow]OFF[/]"

        self.query_one("#temperatures", Static).update(
            "[bold]Arduino Temperatures[/]\n"
            f"Ref:        {fmt(a.ref_C, '°C', 2)}\n"
            f"Heater:     {fmt(a.heater_C, '°C', 2)}\n"
            f"Target:     {fmt(a.target_C, '°C', 2)}\n"
            f"Delta T:    {fmt(a.deltaT_C, '°C', 2)}\n"
            f"Ambient:    {fmt(a.ambient_C, '°C', 2)}\n"
            f"Humidity:   {fmt(a.humidity_pct, '%', 1)}"
        )

        self.query_one("#power", Static).update(
            "[bold]Heater / INA219[/]\n"
            f"PWM:        {fmt(a.pwm, '%', 1)}\n"
            f"Power:      {fmt(a.power_W, 'W', 4)}\n"
            f"Power avg:  {fmt(a.power_avg_W, 'W', 4)}\n"
            f"Current:    {fmt(a.current_mA, 'mA', 2)}\n"
            f"Bus V:      {fmt(a.bus_V, 'V', 3)}\n"
            f"Load V:     {fmt(a.load_V, 'V', 3)}"
        )

        self.query_one("#status_panel", Static).update(
            "[bold]Arduino Status[/]\n"
            f"Alarm:      {alarm_text}\n"
            f"PID:        {pid_text}\n"
            f"Ref mode:   {a.ref_mode}\n"
            f"EL state:   {a.el_state}\n"
            f"Logging:    {logging_text}"
        )

        self.query_one("#detection", Static).update(
            "[bold]Blockage Detection[/]\n"
            f"Baseline:   {baseline_text}\n"
            f"Capture:    {capture_text}\n"
            f"Base power: {fmt(a.baseline_power_W, 'W', 4)}\n"
            f"Change:     {fmt(a.power_change_percent, '%', 2)}\n"
            f"Trigger:    {fmt(a.trigger_level_W, 'W', 4)}\n"
            f"Clear:      {fmt(a.clear_level_W, 'W', 4)}"
        )

        elapsed = a.t_s - self.t0 if self.t0 is not None and math.isfinite(a.t_s) else math.nan

        self.query_one("#serial_info", Static).update(
            "[bold]Serial[/]\n"
            f"Arduino:    {self.arduino_port or ('demo' if self.demo else 'disabled')}\n"
            f"Ard baud:   {self.arduino_baud}\n"
            f"Ard cols:   {a.columns}\n"
            f"Ard lines:  {self.arduino_line_count}\n"
            f"Ard valid:  {self.arduino_valid_count}\n"
            f"Ard errors: {self.arduino_error_count}\n"
            f"Ard age:    {fmt(arduino_age_s, 's', 2)}\n"
            f"Run time:   {fmt(elapsed, 's', 1)}"
        )

        mfc_connected_text = "[green bold]CONNECTED[/]" if m.valid else "[yellow]WAITING[/]"
        self.query_one("#mfc_panel", Static).update(
            "[bold]MFC / Alicat[/]\n"
            f"State:      {mfc_connected_text}\n"
            f"Port:       {self.mfc_port or ('demo' if self.demo else 'disabled')}\n"
            f"Baud:       {self.mfc_baud}\n"
            f"Unit ID:    {m.unit_id}\n"
            f"Pressure:   {fmt(m.pressure, '', 2)}\n"
            f"Temp:       {fmt(m.temperature_C, '°C', 2)}\n"
            f"Vol flow:   {fmt(m.volumetric_flow, 'SLPM?', 3)}\n"
            f"Mass flow:  {fmt(m.mass_flow, 'SLPM?', 3)}\n"
            f"Setpoint:   {fmt(m.setpoint, 'SLPM', 3)}\n"
            f"Gas:        {m.gas or '--'}\n"
            f"MFC lines:  {self.mfc_line_count}\n"
            f"MFC valid:  {self.mfc_valid_count}\n"
            f"MFC errors: {self.mfc_error_count}\n"
            f"MFC age:    {fmt(mfc_age_s, 's', 2)}"
        )

        self.query_one("#help_panel", Static).update(
            "[bold]Controls[/]\n"
            "q        quit\n"
            "l        start/stop logging\n"
            "r        reset displayed run time\n"
            "p        send PID toggle placeholder\n"
            "b        capture baseline placeholder\n"
            "c        clear baseline placeholder\n"
            "m        MFC setpoint placeholder\n"
            "Enter    send command box text\n"
        )

        self.query_one("#notes_panel", Static).update(
            "[bold]Notes[/]\n"
            "Arduino command strings still need to be mapped\n"
            "from the existing PyQt app / firmware.\n\n"
            "MFC polling uses A + CRLF at 38400 baud.\n"
            "MFC setpoint command is currently assumed ASx.xx\n"
            "but should be confirmed before automated tests."
        )

        status_line = f"Status: {self.status_message}"
        if a.parse_error:
            status_line += f" | Arduino parse: {a.parse_error}"
        if m.parse_error:
            status_line += f" | MFC parse: {m.parse_error}"

        self.query_one("#status", Static).update(status_line)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        command = event.value.strip()
        event.input.value = ""

        if not command:
            return

        if command.lower().startswith("mfc:"):
            mfc_command = command[4:].strip()
            self.mfc_worker.send_raw_command(mfc_command)
        else:
            self.arduino_worker.send_command(command)

    def action_toggle_logging(self) -> None:
        if self.logging_enabled:
            self.close_log()
            self.status_message = "Logging stopped"
        else:
            self.open_log()
            self.status_message = f"Logging started: {self.log_path}"

    def action_reset_start_time(self) -> None:
        with self.arduino_lock:
            a = self.arduino
        self.t0 = a.t_s if math.isfinite(a.t_s) else None
        self.status_message = "Displayed run time reset"

    def action_send_arduino_pid_toggle(self) -> None:
        """
        Placeholder. We need to map this to the real firmware command.

        For now, send PID_TOGGLE. If firmware ignores it, no harm.
        """
        self.arduino_worker.send_command("PID_TOGGLE")

    def action_send_arduino_capture_baseline(self) -> None:
        """
        Placeholder. We need to map this to the real firmware command.
        """
        self.arduino_worker.send_command("CAPTURE_BASELINE")

    def action_send_arduino_clear_baseline(self) -> None:
        """
        Placeholder. We need to map this to the real firmware command.
        """
        self.arduino_worker.send_command("CLEAR_BASELINE")

    def action_mfc_setpoint_prompt(self) -> None:
        """
        Initial placeholder: set MFC to 0.00 SLPM.

        Later we will replace this with a small input workflow.
        """
        self.mfc_worker.set_flow(0.0)

    def open_log(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_path = self.log_dir / f"iota_terminal_log_{stamp}.csv"

        self.log_file = self.log_path.open("w", newline="")
        self.csv_writer = csv.writer(self.log_file)

        self.csv_writer.writerow(
            [
                "wall_time_iso",
                "arduino_millis",
                "arduino_t_s",
                "ref_C",
                "heater_C",
                "ambient_C",
                "humidity_pct",
                "bus_V",
                "shunt_mV",
                "load_V",
                "current_mA",
                "target_C",
                "deltaT_C",
                "pwm",
                "power_W",
                "power_avg_W",
                "power_filt_W",
                "pid_enabled",
                "ref_mode",
                "baseline_ready",
                "baseline_capture_active",
                "alarm",
                "el_state",
                "baseline_power_W",
                "power_change_percent",
                "trigger_level_W",
                "clear_level_W",
                "mfc_pressure",
                "mfc_temperature_C",
                "mfc_volumetric_flow",
                "mfc_mass_flow",
                "mfc_setpoint",
                "mfc_gas",
                "arduino_raw_line",
                "mfc_raw_line",
            ]
        )

        self.logging_enabled = True

    def close_log(self) -> None:
        self.logging_enabled = False

        if self.log_file is not None:
            try:
                self.log_file.flush()
                self.log_file.close()
            except Exception:
                pass

        self.log_file = None
        self.csv_writer = None

    def write_log_row(self) -> None:
        if self.csv_writer is None:
            return

        with self.arduino_lock:
            a = self.arduino
        with self.mfc_lock:
            m = self.mfc

        self.csv_writer.writerow(
            [
                datetime.now().isoformat(timespec="milliseconds"),
                a.millis,
                a.t_s,
                a.ref_C,
                a.heater_C,
                a.ambient_C,
                a.humidity_pct,
                a.bus_V,
                a.shunt_mV,
                a.load_V,
                a.current_mA,
                a.target_C,
                a.deltaT_C,
                a.pwm,
                a.power_W,
                a.power_avg_W,
                a.power_filt_W,
                a.pid_enabled,
                a.ref_mode,
                a.baseline_ready,
                a.baseline_capture_active,
                a.alarm,
                a.el_state,
                a.baseline_power_W,
                a.power_change_percent,
                a.trigger_level_W,
                a.clear_level_W,
                m.pressure,
                m.temperature_C,
                m.volumetric_flow,
                m.mass_flow,
                m.setpoint,
                m.gas,
                a.raw_line,
                m.raw_line,
            ]
        )

        if self.log_file is not None:
            self.log_file.flush()


# =============================================================================
# CLI entry point
# =============================================================================


def main() -> None:
    parser = argparse.ArgumentParser(description="Smart Tubing IOTA terminal test station")

    parser.add_argument("--demo", action="store_true", help="Run demo mode without hardware")

    parser.add_argument("--arduino-port", default=None, help="Arduino serial port, e.g. /dev/ttyACM0")
    parser.add_argument("--arduino-baud", type=int, default=115200, help="Arduino baud rate")

    parser.add_argument("--mfc-port", default=None, help="MFC serial port, e.g. /dev/ttyUSB0")
    parser.add_argument("--mfc-baud", type=int, default=38400, help="MFC baud rate")
    parser.add_argument("--mfc-unit-id", default="A", help="MFC unit ID, default A")

    parser.add_argument(
        "--log-dir",
        default="logs",
        help="Directory for CSV logs. Default: ./logs",
    )

    args = parser.parse_args()

    app = IotaTermApp(
        arduino_port=args.arduino_port,
        arduino_baud=args.arduino_baud,
        mfc_port=args.mfc_port,
        mfc_baud=args.mfc_baud,
        mfc_unit_id=args.mfc_unit_id,
        demo=args.demo,
        log_dir=Path(args.log_dir).expanduser().resolve(),
    )
    app.run()


if __name__ == "__main__":
    main()
