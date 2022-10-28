#!/usr/bin/python3
"""
    LifX Smart Lights Script: evening schedule
    Copyright (C) 2022 Josh Simonot

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    See see <https://www.gnu.org/licenses/> for a copy of the GNU
    General Public License
"""

# depends on: pip install lifxlan
# https://github.com/mclarkk/lifxlan

import sys
import time
import requests

from datetime import datetime, timedelta, timezone
from datetime import time as _time
from datetime import date as _date

from lifxlan import LifxLAN
from lifxlan import Group as LifxGroup
from lifxlan import Device as LifXDevice

# Power states
POWER_ON = True
POWER_OFF = False


def utc_now() -> datetime:
    """All datetime values used must be in UTC. The exception
    being user input/output which will be parsed/formated in
    the user's local time"""
    return datetime.now(timezone.utc)


def utc_datetime(date: _date, time: _time) -> datetime:
    """Construct a UTC datetime from user's local date and time"""
    return datetime.combine(date, time).astimezone(timezone.utc)


def sleep_until(then: datetime, timeout: timedelta = None) -> None:
    """Sleeps until a specified UTC datetime or raises TimeoutError
    after timing out"""
    wait_time = then - utc_now()
    if timeout and timeout < wait_time:
        time.sleep(timeout.total_seconds())
        raise TimeoutError
    if wait_time > timedelta(0):
        time.sleep(wait_time.total_seconds())


def request_sunrise_sunset(lat: float, lng: float, date: datetime):
    """Uses service provided by https://sunrise-sunset.org/api
    Retrieves the sunrise and sunset times in UTC for a given UTC date
    """
    url = "http://api.sunrise-sunset.org/json"
    date_str = date.strftime("%Y-%m-%d")
    params = f"?lat={lat}&lng={lng}&date={date_str}&formatted=0"

    # expect exception if request fails, parsing json fails,
    # there are no results, or date format changes
    payload = requests.get(url+params).json()["results"]
    sunrise = datetime.strptime(payload["sunrise"], "%Y-%m-%dT%H:%M:%S%z")
    sunset = datetime.strptime(payload["sunset"], "%Y-%m-%dT%H:%M:%S%z")
    return (sunrise, sunset)


def get_sunset_or_default(lat: float, lng: float, date: datetime) -> datetime:
    """Get sunset datetime from request or default time if request
    fails for any reason"""
    try:
        _, sunset = request_sunrise_sunset(lat, lng, date)
        return sunset
    except Exception as e:
        print("warning: exception raised while requesting sunrise-sunset")
        print(e)
        # fallback to default time of 6PM
        local_date = date.astimezone().date
        local_time = _time(hour=18, minute=0, second=0)
        return utc_datetime(local_date, local_time)


def next_sunset(lat: float, lng: float) -> datetime:
    """Returns today's sunset datetime if it has not already
    passed. If it has passed, then returns tomorrow's sunset
    """
    now = utc_now()
    sunset = get_sunset_or_default(lat, lng, now)
    if sunset > now:
        return sunset
    return get_sunset_or_default(lat, lng, now + timedelta(days=1))


def already_in_group(device: LifXDevice, group: LifxGroup) -> bool:
    """Checks if a device has already been added to the group"""
    group_addresses = [dev.mac_addr for dev in group.get_device_list()]
    return device.mac_addr in group_addresses


class LampState:
    """Defines the state of a LifX Light or LifX Group as (power, color)"""

    def __init__(self, name: str, power: bool, color: tuple) -> None:
        self.name = name
        self.power = power
        self.color = color

    def apply(self, lights: LifxGroup, duration: timedelta) -> None:
        """Sends commands to the lights to apply power and color settings.
        The duration parameter specifies the length of time the light should
        take to transition to the new settings
        """
        duration_ms = int(1000 * duration.total_seconds())
        if self.power:
            lights.set_power("on", duration_ms)
            time.sleep(0.25)
            lights.set_color(self.color, duration_ms)
        else:
            lights.set_color(self.color, duration_ms)
            time.sleep(0.25)
            lights.set_power("off", duration_ms)

    def equals(self, other) -> bool:
        return self.power == other.power and self.color == other.color


class TimeEvent:
    """An event that triggers a transition to a new state"""

    def __init__(self,
                 name: str,
                 time_utc: datetime,
                 state: LampState,
                 fade: timedelta = timedelta(seconds=4)) -> None:
        self.name = name
        self.time = time_utc
        self.state = state
        self.fade = fade

    def __lt__(self, other) -> bool:
        return self.time.__lt__(other.time)

    def print(self) -> None:
        print(f"Timed Event: {self.name} "
              f"at {self.time.astimezone()} --> {self.state.name}")

    def trigger(self, lights: LifxGroup, fade: timedelta = None) -> None:
        self.print()
        if not fade:
            fade = self.fade
        self.state.apply(lights, fade)


class Timeline:
    """Maintains a sorted list of TimeEvents and allows waiting for
    the next TimeEvent"""

    def __init__(self) -> None:
        self.timeline = []

    def insert(self, ev: TimeEvent) -> None:
        self.timeline.append(ev)
        self.timeline.sort()

    def pop(self, timeout: timedelta = None) -> TimeEvent:
        """Sleeps until the next TimeEvent and then returns it,
        If a TimeEvent has already passed, it's returned immediately,
        If there are no remaining TimeEvents, IndexError is raised,
        If the wait timeouts, TimeoutError is raised
        """
        next_event = self.timeline[0].time
        sleep_until(next_event, timeout)
        return self.timeline.pop(0)

    def print(self) -> None:
        for ev in self.timeline:
            ev.print()


def save_current_states(devices):
    """save the current state of all devices
    index 0 --> mac address for confirming same device
    index 1 --> power setting
    index 2 --> color setting
    """
    saved_states = []
    for dev in devices:
        saved_states.append((
            dev.mac_addr,
            dev.get_power(),
            dev.get_color()
        ))
    return saved_states


def reset_device_states(devices, saved_states):
    """update device state if they are different
    than the current state"""
    for i, dev in enumerate(devices):
        saved_state = saved_states[i]
        current_state = (dev.mac_addr, dev.get_power(), dev.get_color())

        if current_state[1] != saved_state[1]:
            dev.set_power(saved_state[1])
        if current_state[2] != saved_state[2]:
            dev.set_color(saved_state[2])


def main():
    if len(sys.argv) < 2:
        print("Provide names for the lights and/or groups to control:\n")
        print("  ./evening-lights.py [light_name] [group_name]\n")
        exit(1)

    print("Discovering lights...")
    lifx = LifxLAN()

    # the group of lights this script will control
    ctrl_group = LifxGroup()

    # labels can be for a device or group
    labels = sys.argv[1:]

    for label in labels:
        # check for group name
        group = lifx.get_devices_by_group(label)
        devices = group.get_device_list()
        if len(devices) > 0:
            for dev in devices:
                if not already_in_group(dev, ctrl_group):
                    ctrl_group.add_device(dev)
        else:
            # check for device name
            dev = lifx.get_device_by_name(label)
            if dev:
                if not already_in_group(dev, ctrl_group):
                    ctrl_group.add_device(dev)
            else:
                print(f"No devices found matching label '{label}'")

    devices = ctrl_group.get_device_list()
    print(f"Found {len(devices)} device(s)")

    if len(devices) == 0:
        exit(1)

    lights = [dev for dev in devices if dev.is_light()]
    if len(lights) == 0:
        print("None of the devices found are lights")
        exit(1)

    # save the current state of all lights
    saved_states = save_current_states(devices)

    # timeline will be filled below
    timeline = Timeline()

    print("Running...")
    while True:
        try:
            """wait for the next TimeEvent and then
            trigger transition into new LampState"""
            event = timeline.pop(timeout=None)
            event.trigger(ctrl_group)

        except IndexError:
            """the timeline is empty and needs to be filled
            with the next set of TimeEvents"""
            fill_timeline(timeline)

        except TimeoutError:
            """set a timeout to have the option to take periodic
            actions while waiting for the next TimeEvent"""
            pass

        except KeyboardInterrupt:
            """return all lights back to how we found them"""
            reset_device_states(devices, saved_states)
            break


###############################################################################
# Configure and schedule event timeline:
#   - define a set of lamp states,
#   - define a set of TimeEvents that trigger transitions into lamp states
#   - add TimeEvents to Timeline
###############################################################################

def fill_timeline(timeline: Timeline) -> None:
    # color states (HUE, SATURATION, BRIGHTNESS, TEMPERATURE[Kelvin])
    COLOR_NIGHT_LIGHT = (8402, 0, 49151, 2000)
    COLOR_NEUTRAL_LIGHT = (8402, 0, 65535, 3500)

    # config
    SUNSET_OFFSET = timedelta(minutes=-30)
    TRANSITION_DURATION = timedelta(minutes=5)
    LATITUDE, LONGITUDE = (45.42178, -75.69119)

    # create valid states
    state_evening = LampState("Evening Lights", POWER_ON, COLOR_NEUTRAL_LIGHT)
    state_night = LampState("Night Lights", POWER_ON, COLOR_NIGHT_LIGHT)
    state_off = LampState("Lights Off", POWER_OFF, COLOR_NIGHT_LIGHT)

    fade = TRANSITION_DURATION

    # makes a request to the free api: https://sunrise-sunset.org/api
    # be nice and try to only call it once per day
    sunset = next_sunset(LATITUDE, LONGITUDE)
    local_date = sunset.astimezone().date()

    # transition into evening lights a bit before sunset
    evening = sunset + SUNSET_OFFSET
    timeline.insert(TimeEvent("Evening", evening, state_evening, fade))

    # transition into night lights at 9PM
    nighttime = utc_datetime(local_date, _time(hour=21))
    timeline.insert(TimeEvent("Nightime", nighttime, state_night, fade))

    # turn lights off at midnight (9PM + 3hrs)
    lightsoff = nighttime + timedelta(hours=3)
    timeline.insert(TimeEvent("Lights Off", lightsoff, state_off, fade))

    print("Queued events:")
    timeline.print()
    print("--")

###############################################################################
###############################################################################


if __name__ == "__main__":
    main()
