# Copyright 2020 Andrew Bates
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Platform for sensor integration."""
import logging
from datetime import timedelta

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.const import ATTR_DATE
from homeassistant.const import ATTR_NAME
from homeassistant.const import ATTR_TIME
from homeassistant.const import CONF_CONDITION
from homeassistant.core import callback
from homeassistant.core import HomeAssistant
from homeassistant.helpers import condition
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from . import ATTR_DATE_TEMPLATE
from . import ATTR_SCHEDULE
from . import ATTR_SCHEDULES
from . import ATTR_TIME_TEMPLATE
from . import parse_date
from . import parse_time
from .schedule import DateSlot
from .schedule import Schedule
from .schedule import TimeSlot

_LOGGER = logging.getLogger(__name__)

_TIME_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required(ATTR_NAME): str,
            vol.Exclusive(ATTR_TIME, "time"): vol.All(
                vol.Datetime(format="%M:%S"), parse_time
            ),
            vol.Exclusive(ATTR_TIME_TEMPLATE, "time"): cv.template,
        }
    ),
    cv.has_at_least_one_key(ATTR_TIME, ATTR_TIME_TEMPLATE),
    TimeSlot.from_config,
)


_DATE_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required(ATTR_NAME): str,
            vol.Exclusive(ATTR_DATE, "date"): parse_date,
            vol.Exclusive(ATTR_DATE_TEMPLATE, "date"): cv.template,
        }
    ),
    cv.has_at_least_one_key(ATTR_DATE, ATTR_DATE_TEMPLATE),
    DateSlot.from_config,
)

_SCHEDULE_SCHEMA = vol.Or(
    vol.Schema(
        {
            vol.Optional(ATTR_NAME): str,
            vol.Optional(CONF_CONDITION): cv.CONDITION_SCHEMA,
            vol.Required(ATTR_SCHEDULE): vol.Any([_TIME_SCHEMA], [_DATE_SCHEMA]),
        }
    ),
    vol.Any([_TIME_SCHEMA], [_DATE_SCHEMA]),
)

PLATFORM_SCHEMA = cv.PLATFORM_SCHEMA.extend(
    {
        vol.Required(ATTR_NAME): str,
        vol.Exclusive(ATTR_SCHEDULES, "schedule"): [_SCHEDULE_SCHEMA],
        vol.Exclusive(ATTR_SCHEDULE, "schedule"): _SCHEDULE_SCHEMA,
    }
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    # pylint: disable=unused-argument
    discovery_info,
):
    """Set up the sensor platform."""
    scheds_config = config.get(ATTR_SCHEDULES)
    schedules = []
    if scheds_config is None:
        _LOGGER.debug("Adding schedule %s", config.get(ATTR_SCHEDULE))
        schedules = [Schedule(hass, None, None, config.get(ATTR_SCHEDULE))]
    else:
        for sched_config in scheds_config:
            if_cond = None
            if sched_config.get(CONF_CONDITION) is not None:
                if_cond = await condition.async_from_config(
                    hass, sched_config.get(CONF_CONDITION)
                )
            _LOGGER.debug("Adding schedule %s", sched_config.get(ATTR_SCHEDULE))
            schedules.append(
                Schedule(
                    hass,
                    sched_config.get(ATTR_NAME),
                    if_cond,
                    sched_config.get(ATTR_SCHEDULE),
                )
            )

    sensor = ScheduleSensor(hass, config[ATTR_NAME], schedules)
    async_add_entities([sensor])


class ScheduleSensor(Entity):
    """Sensor that presents the current slot for a configured schedule."""

    def __init__(self, hass: HomeAssistant, name: str, schedules: list[Schedule]):
        """Initialize the sensor."""
        self.hass = hass
        self._name = name
        self._state = None
        self.schedules = schedules
        self.active_schedule: Schedule = None
        self._next_update = dt_util.utcnow() + timedelta(seconds=10)
        self.logger = logging.getLogger(f"{__name__}.{name}")

        self._update_internal_state(dt_util.utcnow())

    def _calculate_next_update(self):
        """Determine the next time the sensor should be updated"""
        now = dt_util.utcnow()
        # default to 10 seconds to accomodate schedules not
        # being ready when Homeassistant restarts
        delta = 10
        if self.active_schedule is not None:
            interval = self.active_schedule.interval
            timestamp = int(dt_util.as_timestamp(now))
            delta = interval - (timestamp % interval)
        self._next_update = now + timedelta(seconds=delta)
        return self._next_update

    @property
    def unique_id(self):
        return self._name

    @property
    def next_update(self):
        """The next time this sensor should be updated"""
        return self._next_update

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def state(self):
        """Return the state of the sensor."""
        return self._state

    def _calculate_active_schedule(self):
        """Fetch the current active schedule or None if there are no active schedules"""
        self.active_schedule = None
        for schedule in self.schedules:
            # Get the currently active schedule
            if schedule.active:
                self.active_schedule = schedule
                return

    def _update_internal_state(self, date_time):
        """Fetch new state data for the sensor."""
        self._calculate_active_schedule()
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("Updating state")
            if self.active_schedule is None:
                _LOGGER.debug("No active schedule")
            _LOGGER.debug("Next update %s", self.next_update)

        if self.active_schedule is not None:
            self.active_schedule.update(date_time)
            self._state = self.active_schedule.state

    @callback
    def point_in_time_listener(self, date_time):
        """Get the active schedule slot and update the state."""
        self._update_internal_state(date_time)
        self.async_schedule_update_ha_state()
        async_track_point_in_utc_time(
            self.hass, self.point_in_time_listener, self._calculate_next_update()
        )

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        async_track_point_in_utc_time(
            self.hass, self.point_in_time_listener, self._calculate_next_update()
        )
