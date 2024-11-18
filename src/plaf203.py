from __future__ import annotations

import appdaemon.adapi as adapi
import appdaemon.adbase as adbase
import appdaemon.plugins.hass.hassapi as hassapi
import appdaemon.plugins.mqtt.mqttapi as mqttapi

from dataclasses import dataclass
import datetime
import enum
import hashlib
import json
import time
import uuid

########################################################################################################################

# Default audio url: https://dl-oss-prod.s3.us-east-1.amazonaws.com/platform/audio/come_to_eat.aac

# Remark regarding audio: The device tries to download the audio file and will crash and burn (i.e. reboot) if it fails
# to do so without any further error output to the user

# Currently known issues
# - Setting the audio URL doesn't work for me and crashes the device -> Turn off feeding audio

# Reverse engineered protocol of firmware
# Hardware version: 1.0.7
# Software version: 3.0.14

# All "commands" of the protocol. Every message has a "cmd" field that essentially identifies the
# type of message in order to convert it to the right type of data structure and dispatch
# the message correctly.
# Some commands yield uni-directional messages, others/most are bi-directional. This is important
# as the device is also waiting to receive a response to a request it issued. Not receiving the
# response can result in odd behavior, e.g. things not working, error state

# TODO re-doc this, it's not fully accurate
# There are three groups of commands/messages:
# Product features communication
# - "service": Bi-directional communication, request issued by server to device and device responds
# - "event": Uni-directional communication, message/event issued by device, no response by server
# System features communication
# - any other messages are considered "system level" messages to handle "low level device stuff" like ntp, heartbeat, config, updates, wifi, device reset 
class Commands:
    # Sent by the server to get all device "attributes" of the device. Device attributes are
    # various kinds of status information about the device as well as any product related configuration
    # that control features audio, sound, camera etc.
    # The device responds to this message with the current full attribute list
    ATTR_GET_SERVICE: str = 'ATTR_GET_SERVICE'

    # Sent by device to report to the server any changes in attributes. This is done initially
    # when the device connected to the wifi as well as on response to any actual attribute
    # changes issued by ATTR_SET_SERVICE. The server must respond to this message
    ATTR_PUSH_EVENT: str = 'ATTR_PUSH_EVENT'

    # Sent by the server to change attributes (sparse message allowed) on the device. The device
    # responds to this message and additionally issues a ATTR_PUSH_EVENT message with anything
    # that actually changed
    ATTR_SET_SERVICE: str = 'ATTR_SET_SERVICE'

    # After subscribing to the topics, the device is initially checking if it is already in a
    # "bound state", i.e. has a wifi connection. If not, it will not start with a NTP message
    # but instead issue a BINDING message/request. The device expects a respond. Right now,
    # no idea what means of transport this is using if the device is not connected to a wifi, yet
    # Maybe bluetooth or an open wifi?
    BINDING: str = 'BINDING'

    # Sent by the device to report either sound or video detections (when enabled)
    # The device is not expecing a response by the server
    DETECTION_EVENT: str = 'DETECTION_EVENT'

    # Sent by the server to change mqtt hosts, low/high waterlevels for the water dispenser device
    # and server region for TUTK's peer-to-peer communication
    # This message is not available on software version 3.0.14 but showed up in my log dumps
    # when using the actual petlibro app at the time of playing around
    # DEVICE_CONFIG_SYNC: str = 'DEVICE_CONFIG_SYNC'

    # Sent by the device to report some state variables. Available in software 3.0.14
    # but actually never called. Might be some kind of debug/development function?
    # DEVICE_DATA_EVENT: str = 'DEVICE_DATA_EVENT'

    # Sent by the server to set/change the feeding plan on the device
    # The device responds to the server's message
    DEVICE_FEEDING_PLAN_SERVICE: str = 'DEVICE_FEEDING_PLAN_SERVICE'

    # Sent by the server to get information about the device, e.g. serial number, camera id
    # The device responds to the request issued by the server
    DEVICE_INFO_SERVICE: str = 'DEVICE_INFO_SERVICE'

    # Sent by the server to get the water pump state of the device (naturally, not relevant for the feeder)
    # The device responds to the request issued by the server
    DEVICE_PROPERTIES_SERVICE: str = 'DEVICE_PROPERTIES_SERVICE'

    # Sent by the server to remote reboot the device
    # The device responds to the request issued by the server before rebooting
    DEVICE_REBOOT: str = 'DEVICE_REBOOT'

    # When the device successfully (re-) connects to the configured wifi, it will issue this
    # message to tell the server it has started.
    # The device awaits a response by the server to this message
    DEVICE_START_EVENT: str = 'DEVICE_START_EVENT'

    # Sent by the device to tell the server something went wrong.
    # The device expects a response to this message
    ERROR_EVENT: str = 'ERROR_EVENT'

    # Sent by the server to set/change the feeding plan on the device
    # The device responds to the server's message
    # TODO same as DEVICE_FEEDING_PLAN_SERVICE but DEVICE_FEEDING_PLAN_SERVICE isn't used
    # on my version of the feeder?
    FEEDING_PLAN_SERVICE: str = 'FEEDING_PLAN_SERVICE'

    # Sent by the server to the current's device configuration/version information
    # The device responds to the request by the server
    GET_CONFIG: str = 'GET_CONFIG'

    # Sent by the device to request the current feeding plan from the server
    # The server responds to this message with the current feeding plan
    GET_FEEDING_PLAN_EVENT: str = 'GET_FEEDING_PLAN_EVENT'

    # Sent by the device to tell the server about the current state of outputting food
    # The server responds to this message
    GRAIN_OUTPUT_EVENT: str = 'GRAIN_OUTPUT_EVENT'

    # Sent by the device to the server to indicate the device is still around.
    # The device sends this periodically every ~50-60 seconds
    # The server does NOT respond to this message
    HEARTBEAT: str = 'HEARTBEAT'

    # Sent by the server to the device to (re-) initialize/format the SD card
    # The device responds to this message
    INITIALIZE_SD_CARD_SERVICE: str = 'INITIALIZE_SD_CARD_SERVICE'

    # Sent by the server to the device to initiate "manual" food output
    # The device responds to this message
    MANUAL_FEEDING_SERVICE: str = 'MANUAL_FEEDING_SERVICE'

    # Sent by the device to check if the device's clock and timezone are configured correctly.
    # Naturally, this is relevant to correct execution of the configured feeding plans. The
    # server checks the time received and either reports back that everything's fine or
    # issues a time and timezone recalibration with the response
    # Another NTP request is issued by the device whenever a feeding plan executes.
    NTP: str = 'NTP'

    # The server can issue a separate "force sync" request to the client. With timestamps
    # included in every message, the server can keep checking if the time changed or
    # drifted too much, then issue this type of request to enforce an immediate time
    # and timezone reconfiguration
    NTP_SYNC: str = 'NTP_SYNC'

    # Sent by the device to the server to inform about OTA
    # The device expects a response to this message by the server
    OTA_INFORM: str = 'OTA_INFORM'

    # Sent by the device to report the update progress
    # The device expects a response to this message by the server
    OTA_PROGRESS: str = 'OTA_PROGRESS'

    # Sent by the server to initiate a OTA upgrade
    # The device responds to this message
    OTA_UPGRADE: str = 'OTA_UPGRADE'

    # Sent by the device to tell the server the device is being factory resetted by the user
    # The device expects a reponse by the server to this message
    RESET: str = 'RESET'

    # Sent by the server to remote factory restore the device settings
    # The device responds to this message
    RESTORE: str = 'RESTORE'

    # Sent by the server, no response by the device
    # Current purpose unknown
    SERVER_CONFIG_PUSH: str = 'SERVER_CONFIG_PUSH'

    # Sent by the server to update settings related to the TUTK API for video and audio streaming for the camera model of the feeder
    # The device responds to this message
    TUTK_CONTRACT_SERVICE: str = 'TUTK_CONTRACT_SERVICE'

    # Sent by the server to the device to "unbind" the device
    # The device only responds to this if unbinding is not successful
    UNBIND: str = 'UNBIND'

    # Sent by the server to the device to change the wifi configuration
    # The device only responds to this if changing the configuration fails
    WIFI_CHANGE_SERVICE: str = 'WIFI_CHANGE_SERVICE'

    # Sent by the server to remote force a wifi reconnect on the device
    # The device responds to this message
    WIFI_RECONNECT_SERVICE: str = 'WIFI_RECONNECT_SERVICE'

# Device firmware implementation remark:
# Topics are only used to group the different types of message streams on
# the broker level. The device just subscribes to all topics using a single
# callback handler and then only cares about the command type it receives
# to dispatch any messages accordingly
class MessageTopics:
    DEVICE_PRODUCT_ID: str = 'PLAF203'

    def __init__(self, device_serial_number: str):
        self.device_serial_number: str = device_serial_number

    def heart_post_get(self) -> str:
        return self._topic_name_post_get('heart')

    def ota_post_get(self) -> str:
        return self._topic_name_post_get('ota')

    def ota_sub_get(self) -> str:
        return self._topic_name_sub_get('ota')

    def ntp_post_get(self) -> str:
        return self._topic_name_post_get('ntp')

    def ntp_sub_get(self) -> str:
        return self._topic_name_sub_get('ntp')

    def broadcast_sub_get(self) -> str:
        return self._topic_name_sub_get('broadcast')

    def config_post_get(self) -> str:
        return self._topic_name_post_get('config')

    def config_sub_get(self) -> str:
        return self._topic_name_sub_get('config')

    def event_post_get(self) -> str:
        return self._topic_name_post_get('event')

    def event_sub_get(self) -> str:
        return self._topic_name_sub_get('event')
    
    def service_post_get(self) -> str:
        return self._topic_name_post_get('service')

    def service_sub_get(self) -> str:
        return self._topic_name_sub_get('service')

    def system_post_get(self) -> str:
        return self._topic_name_post_get('system')

    def system_sub_get(self) -> str:
        return self._topic_name_sub_get('system')

    def _topic_name_sub_get(self, endpoint: str) -> str:
        # "Direction" of the topic name is from the client's perspective
        # i.e. sub = client subscribes and receives messages
        return "dl/{}/{}/device/{}/sub".format(self.DEVICE_PRODUCT_ID, self.device_serial_number, endpoint) 

    def _topic_name_post_get(self, endpoint: str) -> str:
        # "Direction" of the topic name is from the client's perspective
        # i.e. post = client publishes and sends messages
        return "dl/{}/{}/device/{}/post".format(self.DEVICE_PRODUCT_ID, self.device_serial_number, endpoint) 

@dataclass
class MessageId:
    data: str

    @staticmethod
    def generate() -> MessageId:
        uuid_random = uuid.uuid4()
        hash_object = hashlib.sha256(str(uuid_random).encode())
    
        return MessageId(hash_object.hexdigest()[:32])

# Remark: Timestamps on the device are in ms since epoch
@dataclass
class Timestamp:
    value: datetime.datetime

    @staticmethod
    def now() -> Timestamp:
        return Timestamp(datetime.datetime.now().astimezone())

    @staticmethod
    def from_timestamp_epoch_ms(timestamp_epoch_ms: int) -> Timestamp:
        # Always assume same time zone as backend as that information is not
        # delivered with each message. The backend needs to detect if this
        # is incorrect and adjust the time on the device accordingly
        return Timestamp(datetime.datetime.fromtimestamp(timestamp_epoch_ms / 1000).astimezone())

    def to_timestamp_epoch_ms(self) -> int:
        return int(self.value.timestamp()) * 1000

    def to_timezone_offset_hours(self) -> int:
        return self.value.utcoffset().total_seconds() / 3600

    def abs_delta(self, other: Timestamp) -> datetime.timedelta:
        return abs(self.value - other.value)

class Code(enum.Enum):
    OK = 0
    ERROR_1 = 1
    ERROR_2 = 2
    ERROR_3 = 3
    ERROR_4 = 4

    # Triggers a wifi reset on the device
    # Can be set as code on ATTR_PUSH_EVENT and NTP
    ERROR_DEVICE_NOT_BOUND = 2030

    def is_ok(self):
        return self == Code.OK

    def is_error(self):
        return not self == Code.OK

# Odd name. When set to "SCHEDULED_ENABLED" additional start and end time fields
# are provided to tell the device the hours when the feature is active/inactive
class AgingType(enum.Enum):
    # Not used
    INVALID = 0
    # Scheduling feature is disabled
    NON_SCHEDULED_ENABLED = 1
    SCHEDULED_ENABLED = 2

class NightVision(enum.Enum):
    AUTOMATIC = 0
    OPEN = 1
    CLOSE = 2

class Resolution(enum.Enum):
    P720 = 0
    P1080 = 1

class VideoRecordMode(enum.Enum):
    CONTINUOUS = 0
    MOTION_DETECTION = 1

class MotionDetectionSensitivity(enum.Enum):
    LOW = 0
    MEDIUM = 1
    HIGH = 2

class MotionDetectionRange(enum.Enum):
    SMALL = 0
    MEDIUM = 1
    LARGE = 2

class SoundDetectionSensitivity(enum.Enum):
    LOW = 0
    MEDIUM = 1
    HIGH = 2

class PowerMode(enum.Enum):
    USB = 1
    BATTERY = 2

class PowerType(enum.Enum):
    INVALID = 0
    USB_ONLY = 1
    BATTERY_ONLY = 2
    USB_AND_BATTERY = 3

class SdCardState(enum.Enum):
    NOT_AVAILABLE = 0
    AVAILABLE = 1
    INITIALIZING = 2

class SdCardFileSystem(enum.Enum):
    INVALID = 0
    FAT32 = 1
    FAT = 2
    EXFAT = 3
    NTFS = 4
    UNKNOWN = 5

    @staticmethod
    def from_mqtt_payload_value(value: str) -> SdCardFileSystem:
        if value == 'FAT32':
            return SdCardFileSystem.FAT32
        elif value == 'FAT':
            return SdCardFileSystem.FAT
        elif value == 'EXFAT':
            return SdCardFileSystem.EXFAT
        elif value == 'NTFS':
            return SdCardFileSystem.NTFS
        elif value == 'unknown type':
            return SdCardFileSystem.UNKNOWN
        else:
            return SdCardFileSystem.INVALID

class WifiType(enum.Enum):
    TYPE_0 = 0
    TYPE_1 = 1
    TYPE_2 = 2

class ExecStep(enum.Enum):
    INVALID = 0
    GRAIN_START = 1
    GRAIN_END = 2
    GRAIN_BLOCKING = 3

    @staticmethod
    def from_mqtt_payload_value(value: str) -> ExecStep:
        if value == 'GRAIN_START':
            return ExecStep.GRAIN_START
        elif value == 'GRAIN_END':
            return ExecStep.GRAIN_END
        elif value == 'GRAIN_BLOCKING':
            return ExecStep.GRAIN_BLOCKING
        else:
            return ExecStep.INVALID

class GrainOutputType(enum.Enum):
    INVALID = 0
    FEED_PLAN = 1
    MANUAL_FEED = 2
    MANUAL_FEED_BUTTON = 3

@dataclass
class PercentageInt:
    value: int

    def __init__(self, percentage_value: int):
        if percentage_value < 0 or percentage_value > 100:
            raise ValueError("Incorrect range for percentage value: {}".format(percentage_value))

        self.value = percentage_value

    def value_get(self) -> int:
        return self.value

# The remote device considers all food plan HH:MM timestamps zoned to UTC
# Since the user should be able to configure these in their local timezone,
# this deals with converting the timestamps between UTC and the local timezone
@dataclass
class HourMinTimestamp:
    time: datetime.time

    @staticmethod
    def create_from_local_timezone(hour: int, minute: int) -> HourMinTimestamp:
        # Get system configured timezome
        now = datetime.datetime.now().astimezone()

        return HourMinTimestamp(datetime.time(hour = hour, minute = minute, tzinfo = now.tzinfo))

    @staticmethod
    def create_from_utc(hour: int, minute: int):
        utc_time = datetime.time(hour = hour, minute = minute, tzinfo = datetime.timezone.utc)

        utc_datetime = datetime.date.today().astimezone(datetime.timezone.utc)
        utc_remote_datetime = datetime.datetime.combine(utc_datetime, utc_time)

        # Get system configured timezome
        now = datetime.datetime.now().astimezone()

        return HourMinTimestamp(utc_remote_datetime.astimezone(now.tzinfo).time())

    @staticmethod
    def from_dict(data: dict) -> HourMinTimestamp:
        return HourMinTimestamp.create_from_local_timezone(
            hour = int(data['hour']),
            minute = int(data['minute']))

    def to_dict(self) -> dict:
        return {
            'hour': self.time.hour,
            'minute': self.time.minute,
        }

    def to_mqtt_payload_value(self) -> str:
        utc_time = self._time_to_utc_timezone()

        return "{:02}:{:02}".format(utc_time.hour, utc_time.minute)

    @staticmethod
    def from_mqtt_payload_value(value: str) -> HourMinTimestamp:
        hour, minute = map(int, time_string.split(':'))

        return HourMinTimestamp.create_from_utc(hour, minute)

    def _time_to_utc_timezone(self) -> datetime.time:
        local_datetime = datetime.datetime.combine(datetime.date.today(), self.time)

        return local_datetime.astimezone(datetime.timezone.utc).time()


class Weekday(enum.Enum):
    INVALID = 0
    MONDAY = 1
    TUESDAY = 2
    WEDNESDAY = 3
    THURSDAY = 4
    FRIDAY = 5
    SATURDAY = 6
    SUNDAY = 7

@dataclass
class WeekdaySchedule:
    value: set[Weekday]

    @staticmethod
    def create(*args: Weekday) -> WeekdaySchedule:
        return WeekdaySchedule(set(args))

    @staticmethod
    def from_list(data: [str]) -> WeekdaySchedule:
        weekdays: [Weekday] = []

        for item in data:
            weekdays.append(Weekday[item])

        return WeekdaySchedule(weekdays)

    def to_list(self) -> [str]:
        data: [str] = []

        for item in self.value:
            data.append(item.name)

        return data

    def to_mqtt_payload_value(self) -> [int]:
        data: [int] = []

        for v in self.value:
            data.append(v.value)

        # Pad the array with 0s up to the full length of 7 elements
        data.extend([0] * (7 - len(data)))

        return data

    @staticmethod
    def from_mqtt_payload_value(value: [int]) -> WeekdaySchedule:
        weekday_schedule = WeekdaySchedule()
        
        for v in value:
            weekday_schedule.set(Weekday(v))

        return weekday_schedule

# No HeartbeatOut message
@dataclass
class HeartbeatIn:
    # No msgId on this message
    timestamp: Timestamp
    count: int
    rssi: int
    wifi_type: WifiType

    @staticmethod
    def from_mqtt_payload(payload: dict) -> HeartbeatIn:
        return HeartbeatIn(
            timestamp = Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            count = int(payload['count']),
            rssi = int(payload['rssi']),
            wifi_type = WifiType(int(payload['wifiType'])))

@dataclass
class NtpIn:
    # No msgId on this message

    # The timestamp is the current time on the device and might need re-calibration
    # So it is considered part of the actual "payload" in this case and not just
    # metadata
    # As there is no timezone provided, we assume it is the current timezone
    # of the backend. If that's not correct, we just start a calibration
    # process to fix that
    timestamp: Timestamp

    @staticmethod
    def from_mqtt_payload(payload: dict) -> NtpIn:
        return NtpIn(
            timestamp = Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
        )

@dataclass
class NtpOut:
    code: Code
    timestamp: Timestamp
    calibration_tag: bool

    def to_mqtt_payload(self) -> dict:
        return {
            # Payload does not have a message id
            'cmd': Commands.NTP,
            # ts + timezone are used to set time and zone if calibration tag is true
            'ts': self.timestamp.to_timestamp_epoch_ms(),
            'code': self.code.value,
            'calibrationTag': self.calibration_tag,
            'timezone': self.timestamp.to_timezone_offset_hours(),
        }

@dataclass
class NtpSyncIn:
    message_id: MessageId
    timestamp: Timestamp
    code: Code

    @staticmethod
    def from_mqtt_payload(payload: dict) -> NtpSyncIn:
        return NtpSyncIn(
            message_id = MessageId(payload['msgId']),
            timestamp = Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            code = Code(int(payload['code'])),
        )

@dataclass
class NtpSyncOut:
    message_id: MessageId
    timestamp: Timestamp

    @staticmethod
    def create() -> NtpSyncOut:
        return NtpSyncOut(
            message_id = MessageId.generate(),
            timestamp = Timestamp.now(),
        )

    def to_mqtt_payload(self) -> dict:
        return {
            'cmd': Commands.NTP_SYNC,
            'msgId': self.message_id.data,
            # ts + timezone are used to set time and zone
            'ts': self.timestamp.to_timestamp_epoch_ms(),
            'timezone': self.timestamp.to_timezone_offset_hours(),
        }

@dataclass
class DeviceStartEventIn:
    message_id: MessageId
    timestamp: Timestamp
    success: bool
    pid: str
    uuid: str
    mac: str
    wpa3: int
    hardware_version: str
    software_version: str

    @staticmethod
    def from_mqtt_payload(payload: dict) -> DeviceStartEventIn:
        return DeviceStartEventIn(
            message_id = MessageId(payload['msgId']),
            timestamp = Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            success = payload['success'],
            pid = payload['pid'],
            uuid = payload['uuid'],
            mac = payload['mac'],
            wpa3 = int(payload['wpa3']),
            hardware_version = payload['hardwareVersion'],
            software_version = payload['softwareVersion']
        )

@dataclass
class DeviceStartEventOut:
    message_id: MessageId
    timestamp: Timestamp
    code: Code

    @staticmethod
    def create(**kwargs) -> DeviceStartEventOut:
        return DeviceStartEventOut(
            timestamp = Timestamp.now(),
            **kwargs
        )

    def to_mqtt_payload(self) -> dict:
        return {
            'cmd': Commands.DEVICE_START_EVENT,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
            'code': self.code.value,
        }

@dataclass
class MqttAddr:
    host: str
    port: int

@dataclass
class DeviceConfigSyncOut:
    message_id: MessageId
    timestamp: Timestamp
    mqtt_addr: [MqttAddr]
    low_water_305: int
    lack_water_305: int
    tutk_p2p_region: str

    @staticmethod
    def create(**kwargs) -> DeviceConfigSyncOut:
        return DeviceConfigSyncOut(
            message_id = MessageId.generate(),
            timestamp = Timestamp.now(),
            **kwargs
        )

    def to_mqtt_payload(self) -> dict:
        mqtt_addrs = []

        for addr in device_config_sync_out.mqtt_addr:
            mqtt_addrs.append({
                'host': addr.host,
                'port': addr.port
            })

        return {
            'cmd': Commands.DEVICE_CONFIG_SYNC,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
            'mqttAddr': mqtt_addrs,
            'lowWater305': self.low_water_305,
            'lackWater305': self.lack_water_305,
            'tutkP2pRegion': self.tutk_p2p_region,
        }

@dataclass
class ManualFeedingServiceIn:
    message_id: MessageId
    timestamp: Timestamp
    code: Code

    @staticmethod
    def from_mqtt_payload(payload: dict) -> ManualFeedingServiceIn:
        return ManualFeedingServiceIn(
            message_id = MessageId(payload['msgId']),
            timestamp = Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            code = Code(int(payload['code'])),
        )

@dataclass
class ManualFeedingServiceOut:
    message_id: MessageId
    timestamp: Timestamp
    grain_num: int

    @staticmethod
    def create(**kwargs) -> ManualFeedingServiceOut:
        return ManualFeedingServiceOut(
            message_id = MessageId.generate(),
            timestamp = Timestamp.now(),
            **kwargs
        )

    def to_mqtt_payload(self) -> dict:
        return {
            'cmd': Commands.MANUAL_FEEDING_SERVICE,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
            'grainNum': self.grain_num,
        }

@dataclass
class GrainOutputEventIn:
    message_id: MessageId
    timestamp: Timestamp
    finished: bool
    type_: GrainOutputType
    actual_grain_num: int
    expected_grain_num: int
    exec_time: Timestamp
    exec_step: ExecStep
    plan_id: Optional[int] = None
    retried: Optional[str] = None

    @staticmethod
    def from_mqtt_payload(payload: dict) -> GrainOutputEventIn:
        data = {
            'message_id': MessageId(payload['msgId']),
            'timestamp': Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            'finished': payload['finished'],
            'type_': GrainOutputType(int(payload['type'])),
            'actual_grain_num': int(payload['actualGrainNum']),
            'expected_grain_num': int(payload['expectGrainNum']),
            'exec_time': Timestamp.from_timestamp_epoch_ms(int(payload['execTime'])),
            'exec_step': ExecStep[payload['execStep']],
        }

        if 'planId' in payload:
            data = data | { 'plan_id': int(payload['planId']) }

        if 'retried' in payload:
            data = data | { 'retried': payload['retried'] }

        return GrainOutputEventIn(**data)

@dataclass
class GrainOutputEventOut:
    message_id: MessageId
    timestamp: Timestamp
    code: Code
    exec_step: ExecStep

    @staticmethod
    def create(**kwargs) -> GrainOutputEventOut:
        return GrainOutputEventOut(
            timestamp = Timestamp.now(),
            **kwargs
        )

    def to_mqtt_payload(self) -> dict:
        return {
            'cmd': Commands.GRAIN_OUTPUT_EVENT,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
            'code': self.code.value,
            'execStep': self.exec_step.name,
        }

@dataclass
class AttrPushEventIn:
    message_id: MessageId
    timestamp: Timestamp

    # Power state
    power_mode: Optional[PowerMode] = None
    power_type: Optional[PowerType] = None
    electric_quantity: Optional[PercentageInt] = None

    # Feeder state
    surplus_grain: bool = None
    motor_state: int = None
    grain_outlet_state: bool = None

    # Audio playback
    enable_audio: Optional[bool] = None
    audio_url: Optional[str] = None # max length 100
    # Also applies to sound output (volume)
    volume: Optional[PercentageInt] = None

    # Control button lights
    light_switch: Optional[bool] = None
    # Clarification remark: No enable_light field
    light_aging_type: Optional[AgingType] = None
    lighting_start_time_utc: Optional[HourMinTimestamp] = None
    lighting_end_time_utc: Optional[HourMinTimestamp] = None
    lighting_times: Optional[int] = None

    # Sound output
    sound_switch: Optional[bool] = None
    enable_sound: Optional[bool] = None
    sound_aging_type: Optional[AgingType] = None
    sound_start_time_utc: Optional[HourMinTimestamp] = None
    sound_end_time_utc: Optional[HourMinTimestamp] = None
    sound_times: Optional[int] = None

    # auto lock buttons?
    auto_change_mode: Optional[bool] = None
    auto_threshold: Optional[int] = None

    # Camera
    camera_switch: Optional[bool] = None
    enable_camera: Optional[bool] = None
    camera_aging_type: Optional[AgingType] = None
    night_vision: Optional[NightVision] = None
    resolution: Optional[Resolution] = None
    camera_start_time_utc: Optional[HourMinTimestamp] = None
    camera_end_time_utc: Optional[HourMinTimestamp] = None

    # Video recording
    video_record_switch: Optional[bool] = None
    enable_video_record: Optional[bool] = None
    sd_card_state: Optional[SdCardState] = None
    sd_card_file_system: Optional[SdCardFileSystem] = None
    sd_card_total_capacity: Optional[int] = None
    sd_card_used_capacity: Optional[int] = None
    video_record_mode: Optional[VideoRecordMode] = None
    video_record_aging_type: Optional[AgingType] = None
    video_record_start_time_utc: Optional[HourMinTimestamp] = None
    video_record_end_time_utc: Optional[HourMinTimestamp] = None
    
    # Feeding video
    feeding_video_switch: Optional[bool] = None
    enable_video_start_feeding_plan: Optional[bool] = None
    enable_video_after_manual_feeding: Optional[bool] = None
    before_feeding_plan_time: Optional[int] = None # time in seconds
    automatic_recording: Optional[int] = None
    after_manual_feeding_time: Optional[int] = None # time in seconds
    video_watermark_switch: Optional[bool] = None

    # Cloud video recording
    cloud_video_record_switch: Optional[bool] = None
    # Saw these in my message dumps when using the official app but not in the firmware
    # cloud_video_record_mode: str = None
    # cloud_video_recording_aging_type: int = None

    # Motion detection
    motion_detection_switch: Optional[bool] = None
    enable_motion_detection: Optional[bool] = None
    motion_detection_aging_type: Optional[AgingType] = None
    motion_detection_range: Optional[MotionDetectionRange] = None
    motion_detection_sensitivity: Optional[MotionDetectionSensitivity] = None
    motion_detection_start_time_utc: Optional[HourMinTimestamp] = None
    motion_detection_end_time_utc: Optional[HourMinTimestamp] = None

    # Sound detection
    sound_detection_switch: Optional[bool] = None
    enable_sound_detection: Optional[bool] = None
    sound_detection_aging_type: Optional[AgingType] = None
    sound_detection_sensitivity: Optional[SoundDetectionSensitivity] = None
    sound_detection_start_time_utc: Optional[HourMinTimestamp] = None
    sound_detection_end_time_utc: Optional[HourMinTimestamp] = None

    @staticmethod
    def from_mqtt_payload(payload: dict) -> AttrPushEventIn:
        # at least a very sparse payload. a full payload is sent once
        # on startup but then only stuff that changed (maybe with a few
        # static ones?). just treat the whole structure sparse to avoid
        # any pitfalls here
        data = {
            'message_id': MessageId(payload['msgId']),
            'timestamp': Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
        }

        if 'powerMode' in payload:
            data = data | { 'power_mode': PowerMode(int(payload['powerMode'])) }
        if 'powerType' in payload:
            data = data | { 'power_type': PowerType(int(payload['powerType'])) }
        if 'electricQuantity' in payload:
            data = data | { 'electric_quantity': PercentageInt(int(payload['electricQuantity'])) }

        if 'surplusGrain' in payload:
            data = data | { 'surplus_grain': payload['surplusGrain'] }
        if 'motorState' in payload:
            data = data | { 'motor_state': int(payload['motorState']) }
        if 'grainOutletState' in payload:
            data = data | { 'grain_outlet_state': payload['grainOutletState'] }

        if 'enableAudio' in payload:
            data = data | { 'enable_audio': payload['enableAudio'] }
        if 'audioUrl' in payload:
            data = data | { 'audio_url': payload['audioUrl'] }
        if 'volume' in payload:
            data = data | { 'volume': PercentageInt(payload['volume']) }

        if 'lightSwitch' in payload:
            data = data | { 'light_switch': payload['lightSwitch'] }
        if 'lightAgingType' in payload:
            data = data | { 'light_aging_type': AgingType(payload['lightAgingType']) }
        if 'lightingStartTimeUtc' in payload:
            data = data | { 'lighting_start_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['lightingStartTimeUtc']) }
        if 'lightingEndTimeUtc' in payload:
            data = data | { 'lighting_end_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['lightingEndTimeUtc']) }
        if 'lightingTimes' in payload:
            data = data | { 'lighting_times': int(payload['lightingTimes']) }


        if 'soundSwitch' in payload:
            data = data | { 'sound_switch': payload['soundSwitch'] }
        if 'enableSound' in payload:
            data = data | { 'enable_sound': payload['enableSound'] }
        if 'soundAgingType' in payload:
            data = data | { 'sound_aging_type': AgingType(payload['soundAgingType']) }
        if 'soundStartTimeUtc' in payload:
            data = data | { 'sound_start_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['soundStartTimeUtc']) }
        if 'soundEndTimeUtc' in payload:
            data = data | { 'sound_end_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['soundEndTimeUtc']) }
        if 'soundTimes' in payload:
            data = data | { 'sound_times': payload['soundTimes'] }

        if 'autoChangeMode' in payload:
            data = data | { 'auto_change_mode': payload['autoChangeMode'] }
        if 'autoThreshold' in payload:
            data = data | { 'auto_threshold': payload['autoThreshold'] }

        if 'cameraSwitch' in payload:
            data = data | { 'camera_switch': payload['cameraSwitch'] }
        if 'enableCamera' in payload:
            data = data | { 'enable_camera': payload['enableCamera'] }
        if 'cameraAgingType' in payload:
            data = data | { 'camera_aging_type': AgingType(payload['cameraAgingType']) }
        if 'nightVision' in payload:
            data = data | { 'night_vision': NightVision[payload['nightVision']] }
        if 'resolution' in payload:
            data = data | { 'resolution': Resolution[payload['resolution']] }
        if 'cameraStartTimeUtc' in payload:
            data = data | { 'camera_start_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['cameraStartTimeUtc']) }
        if 'cameraEndTimeUtc' in payload:
            data = data | { 'camera_end_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['cameraEndTimeUtc']) }

        if 'videoRecordSwitch' in payload:
            data = data | { 'video_record_switch': payload['videoRecordSwitch'] }
        if 'enableVideoRecord' in payload:
            data = data | { 'enable_video_record': payload['enableVideoRecord'] }
        if 'sdCardState' in payload:
            data = data | { 'sd_card_state': SdCardState(payload['sdCardState']) }
        if 'sdCardFileSystem' in payload:
            data = data | { 'sd_card_file_system': SdCardFileSystem.from_mqtt_payload_value(payload['sdCardFileSystem']) }
        if 'sdCardTotalCapacity' in payload:
            data = data | { 'sd_card_total_capacity': payload['sdCardTotalCapacity'] }
        if 'sdCardUsedCapacity' in payload:
            data = data | { 'sd_card_used_capacity': payload['sdCardUsedCapacity'] }

        if 'videoRecordMode' in payload:
            data = data | { 'video_record_mode': VideoRecordMode[payload['videoRecordMode']] }
        if 'videoRecordAgingType' in payload:
            data = data | { 'video_record_aging_type': AgingType(payload['videoRecordAgingType']) }
        if 'videoRecordStartTimeUtc' in payload:
            data = data | { 'video_record_start_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['videoRecordStartTimeUtc']) }
        if 'videoRecordEndTimeUtc' in payload:
            data = data | { 'video_record_end_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['videoRecordEndTimeUtc']) }

        if 'feedingVideoSwitch' in payload:
            data = data | { 'feeding_video_switch': payload['feedingVideoSwitch'] }
        if 'enableVideoStartFeedingPlan' in payload:
            data = data | { 'enable_video_start_feeding_plan': payload['enableVideoStartFeedingPlan'] }
        if 'enableVideoAfterManualFeeding' in payload:
            data = data | { 'enable_video_after_manual_feeding': payload['enableVideoAfterManualFeeding'] }
        if 'beforeFeedingPlanTime' in payload:
            data = data | { 'before_feeding_plan_time': payload['beforeFeedingPlanTime'] }
        if 'automaticRecording' in payload:
            data = data | { 'automatic_recording': payload['automaticRecording'] }
        if 'afterManualFeedingTime' in payload:
            data = data | { 'after_manual_feeding_time': payload['afterManualFeedingTime'] }
        if 'videoWatermarkSwitch' in payload:
            data = data | { 'video_watermark_switch': payload['videoWatermarkSwitch'] }

        if 'cloudVideoRecordSwitch' in payload:
            data = data | { 'cloud_video_record_switch': payload['cloudVideoRecordSwitch'] }
        # if 'cloudVideoRecordMode' in payload:
        #     data = data | { 'cloud_video_record_mode': payload['cloudVideoRecordMode'] }
        # if 'cloudVideoRecordAgingType' in payload:
        #     data = data | { 'cloud_video_recording_aging_type': payload['cloudVideoRecordAgingType'] }

        if 'motionDetectionSwitch' in payload:
            data = data | { 'motion_detection_switch': payload['motionDetectionSwitch'] }
        if 'enableMotionDetection' in payload:
            data = data | { 'enable_motion_detection': payload['enableMotionDetection'] }
        if 'motionDetectionAgingType' in payload:
            data = data | { 'motion_detection_aging_type': AgingType(payload['motionDetectionAgingType']) }
        if 'motionDetectionRange' in payload:
            data = data | { 'motion_detection_range': MotionDetectionRange[payload['motionDetectionRange']] }
        if 'motionDetectionSensitivity' in payload:
            data = data | { 'motion_detection_sensitivity': MotionDetectionSensitivity[payload['motionDetectionSensitivity']] }
        if 'motionDetectionStartTimeUtc' in payload:
            data = data | { 'motion_detection_start_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['motionDetectionStartTimeUtc']) }
        if 'motionDetectionEndTimeUtc' in payload:
            data = data | { 'motion_detection_end_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['motionDetectionEndTimeUtc']) }

        if 'soundDetectionSwitch' in payload:
            data = data | { 'sound_detection_switch': payload['soundDetectionSwitch'] }
        if 'enableSoundDetection' in payload:
            data = data | { 'enable_sound_detection': payload['enableSoundDetection'] }
        if 'soundDetectionAgingType' in payload:
            data = data | { 'sound_detection_aging_type': AgingType(payload['soundDetectionAgingType']) }
        if 'soundDetectionSensitivity' in payload:
            data = data | { 'sound_detection_sensitivity': SoundDetectionSensitivity[payload['soundDetectionSensitivity']] }
        if 'soundDetectionStartTimeUtc' in payload:
            data = data | { 'sound_detection_start_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['soundDetectionStartTimeUtc']) }
        if 'soundDetectionEndTimeUtc' in payload:
            data = data | { 'sound_detection_end_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['soundDetectionEndTimeUtc']) }

        return AttrPushEventIn(**data)

@dataclass
class AttrPushEventOut:
    message_id: MessageId
    timestamp: Timestamp
    code: Code

    @staticmethod
    def create(**kwargs) -> AttrPushEventOut:
        return AttrPushEventOut(
            timestamp = Timestamp.now(),
            **kwargs
        )

    def to_mqtt_payload(self) -> dict:
        return {
            'cmd': Commands.ATTR_PUSH_EVENT,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
            'code': self.code.value,
        }

@dataclass
class AttrSetServiceIn:
    message_id: MessageId
    timestamp: Timestamp
    code: Code

    @staticmethod
    def from_mqtt_payload(payload: dict) -> AttrSetServiceIn:
        return AttrSetServiceIn(
            message_id = MessageId(payload['msgId']),
            timestamp = Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            code = Code(int(payload['code'])),
        )

@dataclass
class AttrSetServiceOut:
    message_id: MessageId
    timestamp: Timestamp

    # Power related stuff
    power_mode: Optional[int] = None

    # Audio playback
    enable_audio: Optional[bool] = None
    audio_url: Optional[str] = None # max length 100
    # Also applies to sound output (volume)
    volume: Optional[PercentageInt] = None

    # Camera
    camera_switch: Optional[bool] = None
    camera_aging_type: Optional[AgingType] = None
    night_vision: Optional[NightVision] = None
    resolution: Optional[Resolution] = None
    camera_start_time_utc: Optional[HourMinTimestamp] = None
    camera_end_time_utc: Optional[HourMinTimestamp] = None

    # Video recording
    video_record_switch: Optional[bool] = None
    video_record_mode: Optional[VideoRecordMode] = None
    video_record_aging_type: Optional[AgingType] = None
    video_record_start_time_utc: Optional[HourMinTimestamp] = None
    video_record_end_time_utc: Optional[HourMinTimestamp] = None
    
    # Feeding video
    feeding_video_switch: Optional[bool] = None
    enable_video_start_feeding_plan: Optional[bool] = None
    enable_video_after_manual_feeding: Optional[bool] = None
    before_feeding_plan_time: Optional[int] = None # time in seconds
    automatic_recording: Optional[int] = None
    after_manual_feeding_time: Optional[int] = None # time in seconds
    video_watermark_switch: Optional[bool] = None

    # Cloud video recording
    cloud_video_record_switch: Optional[bool] = None
    # Saw these in my message dumps when using the official app but not in the firmware
    # cloud_video_record_mode: str = None
    # cloud_video_recording_aging_type: int = None

    # Motion detection
    motion_detection_switch: Optional[bool] = None
    motion_detection_aging_type: Optional[AgingType] = None
    motion_detection_range: Optional[MotionDetectionRange] = None
    motion_detection_sensitivity: Optional[MotionDetectionSensitivity] = None
    motion_detection_start_time_utc: Optional[HourMinTimestamp] = None
    motion_detection_end_time_utc: Optional[HourMinTimestamp] = None

    # Sound detection
    sound_detection_switch: Optional[bool] = None
    sound_detection_aging_type: Optional[AgingType] = None
    sound_detection_sensitivity: Optional[SoundDetectionSensitivity] = None
    sound_detection_start_time_utc: Optional[HourMinTimestamp] = None
    sound_detection_end_time_utc: Optional[HourMinTimestamp] = None

    # Sound output
    sound_switch: Optional[bool] = None
    sound_aging_type: Optional[AgingType] = None
    sound_start_time_utc: Optional[HourMinTimestamp] = None
    sound_end_time_utc: Optional[HourMinTimestamp] = None
    sound_times: Optional[int] = None

    # Control button lights
    light_switch: Optional[bool] = None
    light_aging_type: Optional[AgingType] = None
    lighting_start_time_utc: Optional[HourMinTimestamp] = None
    lighting_end_time_utc: Optional[HourMinTimestamp] = None
    lighting_times: Optional[int] = None

    # auto lock buttons?
    auto_change_mode: Optional[bool] = None
    auto_threshold: Optional[int] = None

    @staticmethod
    def create(**kwargs) -> AttrSetServiceOut:
        return AttrSetServiceOut(
            message_id = MessageId.generate(),
            timestamp = Timestamp.now(),
            **kwargs
        )

    def to_mqtt_payload(self) -> dict:
        payload = {
            'cmd': Commands.ATTR_SET_SERVICE,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
        }

        # Audio playback
        if not self.enable_audio == None:
            payload = payload | { 'enableAudio': 1 if self.enable_audio == True else 0 }
        if not self.audio_url == None:
            payload = payload | { 'audioUrl': self.audio_url }
        if not self.volume == None:
            payload = payload | { 'volume': self.volume.value_get() }

        # Camera
        if not self.camera_switch == None:
            payload = payload | { 'cameraSwitch': self.camera_switch }
        if not self.camera_aging_type == None:
            payload = payload | { 'cameraAgingType': self.camera_aging_type.value }
        if not self.night_vision == None:
            payload = payload | { 'nightVision': self.night_vision.name }
        if not self.resolution == None:
            payload = payload | { 'resolution': self.resolution.name }
        if not self.camera_start_time_utc == None:
            payload = payload | { 'cameraStartTimeUtc': self.camera_start_time_utc.to_mqtt_payload_value() }
        if not self.camera_end_time_utc == None:
            payload = payload | { 'cameraEndTimeUtc': self.camera_end_time_utc.to_mqtt_payload_value() }

        # Video recording
        if not self.video_record_switch == None:
            payload = payload | { 'videoRecordSwitch': self.video_record_switch }
        if not self.video_record_mode == None:
            payload = payload | { 'videoRecordMode': self.video_record_mode.name }
        if not self.video_record_aging_type == None:
            payload = payload | { 'videoRecordAgingType': self.video_record_aging_type.value }
        if not self.video_record_start_time_utc == None:
            payload = payload | { 'videoRecordStartTimeUtc': self.video_record_start_time_utc.to_mqtt_payload_value() }
        if not self.video_record_end_time_utc == None:
            payload = payload | { 'videoRecordEndTimeUtc': self.video_record_end_time_utc.to_mqtt_payload_value() }

        # Feeding video
        if not self.feeding_video_switch == None:
            payload = payload | { 'feedingVideoSwitch': self.feeding_video_switch }
        if not self.enable_video_start_feeding_plan == None:
            payload = payload | { 'enableVideoStartFeedingPlan': self.enable_video_start_feeding_plan }
        if not self.after_manual_feeding_time == None:
            payload = payload | { 'afterManualFeedingTime': self.after_manual_feeding_time }
        if not self.before_feeding_plan_time == None:
            payload = payload | { 'beforeFeedingPlanTime': self.before_feeding_plan_time }
        if not self.automatic_recording == None:
            payload = payload | { 'automaticRecording': self.automatic_recording }
        if not self.enable_video_after_manual_feeding == None:
            payload = payload | { 'enableVideoAfterManualFeeding': self.enable_video_after_manual_feeding }
        if not self.video_watermark_switch == None:
            payload = payload | { 'videoWatermarkSwitch': self.video_watermark_switch }      

        # Cloud video recording
        if not self.cloud_video_record_switch == None:
            payload = payload | { 'cloudVideoRecordSwitch': self.cloud_video_record_switch }
        # if not self.cloud_video_record_mode == None:
        #     payload = payload | { 'cloudVideoRecordMode': self.cloud_video_record_mode }
        # if not self.cloud_video_recording_aging_type == None:
        #     payload = payload | { 'cloudVideoRecordingAgingType': self.cloud_video_recording_aging_type }

        # Motion detection
        if not self.motion_detection_switch == None:
            payload = payload | { 'motionDetectionSwitch': self.motion_detection_switch }
        if not self.motion_detection_aging_type == None:
            payload = payload | { 'motionDetectionAgingType': self.motion_detection_aging_type.value }
        if not self.motion_detection_range == None:
            payload = payload | { 'motionDetectionRange': self.motion_detection_range.name }
        if not self.motion_detection_sensitivity == None:
            payload = payload | { 'motionDetectionSensitivity': self.motion_detection_sensitivity.name }
        if not self.motion_detection_start_time_utc == None:
            payload = payload | { 'motionDetectionStartTimeUtc': self.motion_detection_start_time_utc.to_mqtt_payload_value() }
        if not self.motion_detection_end_time_utc == None:
            payload = payload | { 'motionDetectionEndTimeUtc': self.motion_detection_end_time_utc.to_mqtt_payload_value() }

        # Sound detection
        if not self.sound_detection_switch == None:
            payload = payload | { 'soundDetectionSwitch': self.sound_detection_switch }
        if not self.sound_detection_aging_type == None:
            payload = payload | { 'soundDetectionAgingType': self.sound_detection_aging_type.value }
        if not self.sound_detection_sensitivity == None:
            payload = payload | { 'soundDetectionSensitivity': self.sound_detection_sensitivity.name }
        if not self.sound_detection_start_time_utc == None:
            payload = payload | { 'soundDetectionStartTimeUtc': self.sound_detection_start_time_utc.to_mqtt_payload_value() }
        if not self.sound_detection_end_time_utc == None:
            payload = payload | { 'soundDetectionEndTimeUtc': self.sound_detection_end_time_utc.to_mqtt_payload_value() }

        # Sound output
        if not self.sound_switch == None:
            payload = payload | { 'soundSwitch': self.sound_switch }
        if not self.sound_aging_type == None:
            payload = payload | { 'soundAgingType': self.sound_aging_type.value }
        if not self.sound_start_time_utc == None:
            payload = payload | { 'soundStartTimeUtc': self.sound_start_time_utc.to_mqtt_payload_value() }
        if not self.sound_end_time_utc == None:
            payload = payload | { 'soundEndTimeUtc': self.sound_end_time_utc.to_mqtt_payload_value() }
        if not self.sound_times == None:
            payload = payload | { 'soundTimes': self.sound_times }

        # Control button lights
        if not self.light_switch == None:
            payload = payload | { 'lightSwitch': self.light_switch }
        if not self.light_aging_type == None:
            payload = payload | { 'lightAgingType': self.light_aging_type }
        if not self.lighting_start_time_utc == None:
            payload = payload | { 'lightingStartTimeUtc': self.lighting_start_time_utc.to_mqtt_payload_value() }
        if not self.lighting_end_time_utc == None:
            payload = payload | { 'lightingEndTimeUtc': self.lighting_end_time_utc.to_mqtt_payload_value() }
        if not self.lighting_times == None:
            payload = payload | { 'lightingTimes': self.lighting_times }

        # auto lock buttons?
        if not self.auto_change_mode == None:
            payload = payload | { 'autoChangeMode': self.auto_change_mode }
        if not self.auto_threshold == None:
            payload = payload | { 'autoThreshold': self.auto_threshold }

        return payload

@dataclass
class FeedingPlanIn:
    plan_id: int
    sync_time: Timestamp

@dataclass
class FeedingPlanServiceIn:
    message_id: MessageId
    timestamp: Timestamp
    code: Code
    plans: [FeedingPlanIn]
    # Only available on error, can be either "MsgErro" or "FeedPlanErro"
    msg: Optional[str] = None

    @staticmethod
    def from_pqtt_payload(payload: dict) -> FeedingPlanServiceIn:
        plans: [dict] = payload['plans']
        plans_data: [FeedingPlanIn] = []
        msg: str = None

        for plan in plans:
            plans_data.append(FeedingPlanIn(
                plan_id = plan['planId'],
                sync_time = plan['syncTime']
            ))

        if 'msg' in payload:
            msg = payload['msg']

        return FeedingPlanServiceIn(
            message_id = MessageId(payload['msgId']),
            timestamp = Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            code = Code(int(payload['code'])),
            msg = msg,
            plans = plans_data,
        )

@dataclass
class FeedingPlanOut:
    plan_id: int
    execution_time: HourMinTimestamp
    repeat_day: WeekdaySchedule
    enable_audio: bool
    audio_times: int
    grain_num: int
    sync_time: Timestamp
    skip_end_time: str = None

@dataclass
class FeedingPlanServiceOut:
    message_id: MessageId
    timestamp: Timestamp
    plans: [FeedingPlanOut]

    @staticmethod
    def create(plans: [FeedingPlanOut]) -> FeedingPlanServiceOut:
        return FeedingPlanServiceOut(
            message_id = MessageId.generate(),
            timestamp = Timestamp.now(),
            plans = plans,
        )

    def to_mqtt_payload(self) -> dict:
        plans: [dict] = []

        for plan in self.plans:
            plans.append({
                'planId': plan.plan_id,
                'executionTime': plan.execution_time.to_mqtt_payload_value(),
                'repeatDay': plan.repeat_day.to_mqtt_payload_value(),
                'enableAudio': plan.enable_audio,
                'audioTimes': plan.audio_times,
                'grainNum': plan.grain_num,
                'syncTime': plan.sync_time.to_timestamp_epoch_ms(),
                'skipEndTime': plan.skip_end_time,
            })

        return {
            'cmd': Commands.FEEDING_PLAN_SERVICE,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
            'plans': plans,
        }

@dataclass
class GetFeedingPlanEventIn:
    message_id: MessageId
    timestamp: Timestamp

    @staticmethod
    def from_mqtt_payload(payload: dict) -> GetFeedingPlanEventIn:
        return GetFeedingPlanEventIn(
            message_id = MessageId(payload['msgId']),
            timestamp = Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
        )

@dataclass
class GetFeedingPlanOut:
    plan_id: int
    execution_time: HourMinTimestamp
    repeat_day: WeekdaySchedule
    enable_audio: bool
    audio_times: int
    grain_num: int
    sync_time: Timestamp
    skip_end_time: Optional[str] = None

@dataclass
class GetFeedingPlanEventOut:
    message_id: MessageId
    timestamp: Timestamp
    code: Code
    plans: [GetFeedingPlanOut]

    @staticmethod
    def create(code: Code, plans: [GetFeedingPlanOut]) -> GetFeedingPlanEventOut:
        return GetFeedingPlanEventOut(
            message_id = MessageId.generate(),
            timestamp = Timestamp.now(),
            code = code,
            plans = plans,
        )

    def to_mqtt_payload(self) -> dict:
        plans: [dict] = []

        for plan in self.plans:
            if plan.skip_end_time == None:
                plans.append({
                    'planId': plan.plan_id,
                    'executionTime': plan.execution_time.to_mqtt_payload_value(),
                    'repeatDay': plan.repeat_day.to_mqtt_payload_value(),
                    'enableAudio': plan.enable_audio,
                    'audioTimes': plan.audio_times,
                    'grainNum': plan.grain_num,
                    'syncTime': plan.sync_time.to_timestamp_epoch_ms(),
                })
            else:
                plans.append({
                    'planId': plan.plan_id,
                    'executionTime': plan.execution_time.to_mqtt_payload_value(),
                    'repeatDay': plan.repeat_day.to_mqtt_payload_value(),
                    'enableAudio': plan.enable_audio,
                    'audioTimes': plan.audio_times,
                    'grainNum': plan.grain_num,
                    'syncTime': plan.sync_time.to_timestamp_epoch_ms(),
                    'skipEndTime': plan.skip_end_time,
                })

        return {
            'cmd': Commands.GET_FEEDING_PLAN_EVENT,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
            'code': self.code.value,
            'plans': plans,
        }

@dataclass
class ResetIn:
    message_id: MessageId
    timestamp: Timestamp

    @staticmethod
    def from_mqtt_payload(payload: dict) -> ResetIn:
        return ResetIn(
            message_id = MessageId(payload['msgId']),
            timestamp = Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
        )

@dataclass
class OtaUpgradeIn:
    message_id: MessageId
    timestamp: Timestamp
    code: Code
    error_message: str

    @staticmethod
    def from_mqtt_payload(payload: dict) -> OtaUpgradeIn:
        return OtaUpgradeIn(
            message_id = MessageId(payload['msgId']),
            timestamp = Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            code = Code(int(payload['code'])),
            error_message = payload['errorMsg']
        )

@dataclass
class OtaUpgradeOut:
    message_id: MessageId
    timestamp: Timestamp
    upgrade_type: str
    url: str
    target_software_version: str
    md5: str

    def to_mqtt_payload(self) -> dict:
        return {
            'cmd': Commands.OTA_UPGRADE,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
            'upgradeType': self.upgrade_type,
            'url': self.url,
            'targetSoftwareVersion': self.target_software_version,
            'md5': self.md5,
        }

@dataclass
class OtaProgressIn:
    message_id: MessageId
    timestamp: Timestamp

    progress: str

    @staticmethod
    def from_mqtt_payload(payload: dict) -> OtaProgressIn:
        return OtaProgressIn(
            message_id = MessageId(payload['msgId']),
            timestamp = Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            progress = payload['progress']
        )

@dataclass
class OtaProgressOut:
    message_id: MessageId
    timestamp: Timestamp
    code: Code

    def to_mqtt_payload(self) -> dict:
        return {
            'cmd': Commands.OTA_PROGRESS,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
            'code': self.code.value,
        }

@dataclass
class OtaInformIn:
    message_id: MessageId
    timestamp: Timestamp

    state: str
    error_message: str = None

    @staticmethod
    def from_mqtt_payload(payload: dict) -> OtaInform:
        data = {
            'message_id': MessageId(payload['msgId']),
            'timestamp': Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            'state': payload['state'],
        }

        if 'errorMsg' in payload:
            data = data | { 'error_message': payload['errorMsg'] }

        return OtaInform(**data)

@dataclass
class OtaInformOut:
    message_id: MessageId
    timestamp: Timestamp
    code: Code

    def to_mqtt_payload(self) -> dict:
        return {
            'cmd': Commands.OTA_INFORM,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
            'code': self.code.value,
        }

@dataclass
class ErrorEventIn:
    message_id: MessageId
    timestamp: Timestamp
    error_code: str
    trigger_time: Timestamp

    @staticmethod
    def from_mqtt_payload(payload: dict) -> ErrorEventIn:
        return ErrorEventIn(
            message_id = MessageId(payload['msgId']),
            timestamp = Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            error_code = payload['errorCode'],
            trigger_time = Timestamp.from_timestamp_epoch_ms(int(payload['triggerTime'])),
        )

@dataclass
class ErrorEventOut:
    message_id: MessageId
    timestamp: Timestamp

    def to_mqtt_payload(self) -> dict:
        return {
            'cmd': Commands.ERROR_EVENT,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
        }

@dataclass
class GetConfigIn:
    message_id: MessageId
    timestamp: Timestamp
    product_id: str
    mac_address: str
    hardware_version: str
    software_version: str

    @staticmethod
    def from_mqtt_payload(payload: dict) -> GetConfigIn:
        return GetConfigIn(
            message_id = MessageId(payload['msgId']),
            timestamp = Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            product_id = payload['pid'],
            mac_address = payload['mac'],
            hardware_version = payload['hardwareVersion'],
            software_version = payload['softwareVersion'],
        )

@dataclass
class GetConfigOut:
    message_id: MessageId
    timestamp: Timestamp

    @staticmethod
    def create() -> GetConfigOut:
        return GetConfigOut(
            message_id = MessageId.generate(),
            timestamp = Timestamp.now(),
        )

    def to_mqtt_payload(self) -> dict:
        return {
            'cmd': Commands.GET_CONFIG,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
        }

@dataclass
class AttrGetServiceIn:
    message_id: MessageId
    timestamp: Timestamp
    code: Code

    # Power state
    power_mode: PowerMode
    power_type: PowerType
    electric_quantity: PercentageInt

    # Feeder state
    surplus_grain: bool
    motor_state: int
    grain_outlet_state: bool

    # Wifi
    wifi_ssid: str

    # Audio playback
    enable_audio: bool
    audio_url: str
    # Also applies to sound output (volume)
    volume: PercentageInt

    # Control button lights
    enable_light: bool
    light_switch: bool
    light_aging_type: AgingType

    # Sound output
    enable_sound: bool
    sound_switch: bool
    sound_aging_type: AgingType

    # Automatic button lock?
    auto_change_mode: bool
    auto_threshold: int

    # Camera
    camera_switch: bool
    enable_camera: bool
    camera_aging_type: AgingType
    resolution: Resolution
    night_vision: NightVision

    # Video recording
    video_record_switch: bool
    enable_video_record: bool
    sd_card_state: SdCardState
    video_record_mode: VideoRecordMode
    video_record_aging_type: AgingType

    # Feeding video
    feeding_video_switch: bool
    enable_video_start_feeding_plan: bool
    enable_video_after_manual_feeding: bool
    before_feeding_plan_time: int
    automatic_recording: int
    after_manual_feeding_time: int
    video_watermark_switch: bool

    # Cloud video recording
    cloud_video_record_switch: bool

    # Motion detection
    motion_detection_switch: bool
    enable_motion_detection: bool
    motion_detection_aging_type: AgingType
    motion_detection_sensitivity: MotionDetectionSensitivity
    motion_detection_range: MotionDetectionRange

    # Sound detection
    sound_detection_switch: bool
    enable_sound_detection: bool
    sound_detection_aging_type: AgingType
    sound_detection_sensitivity: SoundDetectionSensitivity

    ### Optionals

    # Control button lights
    lighting_start_time_utc: Optional[HourMinTimestamp] = None
    lighting_end_time_utc: Optional[HourMinTimestamp] = None
    lighting_times: Optional[int] = None

    # Sound output
    sound_start_time_utc: Optional[HourMinTimestamp] = None
    sound_end_time_utc: Optional[HourMinTimestamp] = None
    sound_times: Optional[int] = None

    # Camera
    camera_start_time_utc: Optional[HourMinTimestamp] = None
    camera_end_time_utc: Optional[HourMinTimestamp] = None

    # Video recording
    sd_card_file_system: Optional[SdCardFileSystem] = None
    sd_card_total_capacity: Optional[int] = None
    sd_card_used_capacity: Optional[int] = None
    video_record_start_time_utc: Optional[HourMinTimestamp] = None
    video_record_end_time_utc: Optional[HourMinTimestamp] = None

    # Motion detection
    motion_detection_start_time_utc: Optional[HourMinTimestamp] = None
    motion_detection_end_time_utc: Optional[HourMinTimestamp] = None

    # Sound detection
    sound_detection_start_time_utc: Optional[HourMinTimestamp] = None
    sound_detection_end_time_utc: Optional[HourMinTimestamp] = None

    @staticmethod
    def from_mqtt_payload(payload: dict) -> AttrGetServiceIn:
        data = {
            'message_id': MessageId(payload['msgId']),
            'timestamp': Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            'code': Code(int(payload['code'])),

            'power_mode': PowerMode(int(payload['powerMode'])),
            'power_type': PowerType(int(payload['powerType'])),
            'electric_quantity': PercentageInt(int(payload['electricQuantity'])),

            'surplus_grain': payload['surplusGrain'],
            'motor_state': int(payload['motorState']),
            'grain_outlet_state': payload['grainOutletState'],

            'wifi_ssid': payload['wifiSsid'],

            'enable_audio': False if int(payload['enableAudio']) == 0 else True,
            'audio_url': payload['audioUrl'],
            'volume': PercentageInt(int(payload['volume'])),

            'enable_light': payload['enableLight'],
            'light_switch': payload['lightSwitch'],
            'light_aging_type': AgingType(int(payload['lightAgingType'])),

            'enable_sound': payload['enableSound'],
            'sound_switch': payload['soundSwitch'],
            'sound_aging_type': AgingType(int(payload['soundAgingType'])),

            'auto_change_mode': payload['autoChangeMode'],
            'auto_threshold': int(payload['autoThreshold']),

            'camera_switch': payload['cameraSwitch'],
            'enable_camera': payload['enableCamera'],
            'camera_aging_type': AgingType(int(payload['cameraAgingType'])),
            'resolution': Resolution[payload['resolution']],
            'night_vision': NightVision[payload['nightVision']],

            'video_record_switch': payload['videoRecordSwitch'],
            'enable_video_record': payload['enableVideoRecord'],
            'sd_card_state': SdCardState(int(payload['sdCardState'])),
            'video_record_mode': VideoRecordMode[payload['videoRecordMode']],
            'video_record_aging_type': AgingType(int(payload['videoRecordAgingType'])),

            'feeding_video_switch': payload['feedingVideoSwitch'],
            'enable_video_start_feeding_plan': payload['enableVideoStartFeedingPlan'],
            'enable_video_after_manual_feeding': payload['enableVideoAfterManualFeeding'],
            'before_feeding_plan_time': int(payload['beforeFeedingPlanTime']),
            'automatic_recording': int(payload['automaticRecording']),
            'after_manual_feeding_time': int(payload['afterManualFeedingTime']),
            'video_watermark_switch': payload['videoWatermarkSwitch'],

            'cloud_video_record_switch': payload['cloudVideoRecordSwitch'],

            'motion_detection_switch': payload['motionDetectionSwitch'],
            'enable_motion_detection': payload['enableMotionDetection'],
            'motion_detection_aging_type': AgingType(int(payload['motionDetectionAgingType'])),
            'motion_detection_sensitivity': MotionDetectionSensitivity[payload['motionDetectionSensitivity']],
            'motion_detection_range': MotionDetectionRange[payload['motionDetectionRange']],

            'sound_detection_switch': payload['soundDetectionSwitch'],
            'enable_sound_detection': payload['enableSoundDetection'],
            'sound_detection_aging_type': AgingType(int(payload['soundDetectionAgingType'])),
            'sound_detection_sensitivity': SoundDetectionSensitivity[payload['soundDetectionSensitivity']],
        }

        if 'lightingStartTimeUtc' in payload:
            data = data | { 'lighting_start_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['lightingStartTimeUtc']) }
        if 'lightingEndTimeUtc' in payload:
            data = data | { 'lighting_end_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['lightingEndTimeUtc']) }
        if 'lightingTimes' in payload:
            data = data | { 'lighting_times': int(payload['lightingTimes']) }

        if 'soundStartTimeUtc' in payload:
            data = data | { 'sound_start_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['soundStartTimeUtc']) }
        if 'soundEndTimeUtc' in payload:
            data = data | { 'sound_end_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['soundEndTimeUtc']) }
        if 'soundTimes' in payload:
            data = data | { 'sound_times': int(payload['soundTimes']) }

        if 'cameraStartTimeUtc' in payload:
            data = data | { 'camera_start_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['cameraStartTimeUtc']) }
        if 'cameraEndTimeUtc' in payload:
            data = data | { 'camera_end_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['cameraEndTimeUtc']) }

        if 'sdCardFileSystem' in payload:
            data = data | { 'sd_card_file_system': SdCardFileSystem.from_mqtt_payload_value(payload['sdCardFileSystem']) }
        if 'sdCardTotalCapacity' in payload:
            data = data | { 'sd_card_total_capacity': int(payload['sdCardTotalCapacity']) }
        if 'sdCardUsedCapacity' in payload:
            data = data | { 'sd_card_used_capacity': int(payload['sdCardUsedCapacity']) }
        if 'videoRecordStartTimeUtc' in payload:
            data = data | { 'video_record_start_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['videoRecordStartTimeUtc']) }
        if 'videoRecordEndTimeUtc' in payload:
            data = data | { 'video_record_end_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['videoRecordEndTimeUtc']) }

        if 'motionDetectionStartTimeUtc' in payload:
            data = data | { 'motion_detection_start_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['motionDetectionStartTimeUtc']) }
        if 'motionDetectionEndTimeUtc' in payload:
            data = data | { 'motion_detection_end_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['motionDetectionEndTimeUtc']) }

        if 'soundDetectionStartTimeUtc' in payload:
            data = data | { 'sound_detection_start_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['soundDetectionStartTimeUtc']) }
        if 'soundDetectionEndTimeUtc' in payload:
            data = data | { 'sound_detection_end_time_utc': HourMinTimestamp.from_mqtt_payload_value(payload['soundDetectionEndTimeUtc']) }

        return AttrGetServiceIn(**data)

@dataclass
class AttrGetServiceOut:
    message_id: MessageId
    timestamp: Timestamp

    @staticmethod
    def create() -> AttrGetServiceOut:
        return AttrGetServiceOut(
            message_id = MessageId.generate(),
            timestamp = Timestamp.now(),
        )

    def to_mqtt_payload(self) -> dict:
        return {
            'cmd': Commands.ATTR_GET_SERVICE,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
        }

@dataclass
class DevicePropertiesServiceIn:
    # No msg id
    timestamp: Timestamp
    identifier: str
    success: str

    @staticmethod
    def from_mqtt_payload(payload: dict) -> DevicePropertiesServiceIn:
        return DevicePropertiesServiceIn(
            timestamp = Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            identifier = payload['identifier'],
            success = payload['success']
        )

@dataclass
class DevicePropertiesServiceOut:
    message_id: MessageId
    timestamp: Timestamp
    water_pump_state: str

    def to_mqtt_payload(self) -> dict:
        return {
            'cmd': Commands.DEVICE_PROPERTIES_SERVICE,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
            'waterPumpState': self.water_pump_state,
        }

# TODO same as FeedingPlanServiceIn?
@dataclass
class DeviceFeedingPlanServiceIn:
    message_id: MessageId
    timestamp: Timestamp
    code: Code
    plans: [FeedingPlanIn]
    # Only available on error, can be either "MsgErro" or "FeedPlanErro"
    msg: Optional[str] = None

    @staticmethod
    def from_pqtt_payload(payload: dict) -> DeviceFeedingPlanServiceIn:
        plans: [dict] = payload['plans']
        plans_data: [FeedingPlanIn] = []
        msg: str = None

        for plan in plans:
            plans_data.append(FeedingPlanIn(
                plan_id = plan['planId'],
                sync_type = plan['syncTime']
            ))

        if 'msg' in payload:
            msg = payload['msg']

        return DeviceFeedingPlanServiceIn(
            message_id = MessageId(payload['msgId']),
            timestamp = Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            code = Code(int(payload['code'])),
            msg = msg,
            plans = plans_data,
        )

@dataclass
class DeviceFeedingPlanServiceOut:
    message_id: MessageId
    timestamp: Timestamp

    def to_mqtt_payload(self) -> dict:
        return {
            'cmd': Commands.FEEDING_PLAN_SERVICE,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
        }

@dataclass
class DeviceDataEventIn:
    # No msg id
    timestamp: Timestamp
    identifier: str
    weight: str
    radar_state: str
    water_pump_state: str
    button_state: str

    @staticmethod
    def from_mqtt_payload(payload: dict) -> DeviceDataEventIn:
        return DeviceDataEventIn(
            timestamp = Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            identifier = payload['identifier'],
            weight = payload['weight'],
            radar_state = payload['radar_state'],
            water_pump_state = payload['water_pump_state'],
            button_state = payload['button_state']
        )

@dataclass
class DetectionEventIn:
    message_id: MessageId
    timestamp: Timestamp
    # MOTION or SOUND
    type_: str

    @staticmethod
    def from_mqtt_payload(payload: dict) -> DetectionEventIn:
        return DetectionEventIn(
            message_id = MessageId(payload['msgId']),
            timestamp = Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            type_ = payload['type'],
        )

# Sent by device as its first message instead of NTP if the device is not "bound" to any wifi, yet?
# How are you suppose to receive that message then? Open wifi/bluetooth?
@dataclass
class BindingIn:
    message_id: MessageId
    timestamp: Timestamp
    member_id: str
    type_: str
    product_id: str
    uuid: str
    mac_address: str
    wpa3: str
    hardware_version: str
    software_version: str   

    @staticmethod
    def from_mqtt_payload(payload: dict) -> DetectionEventIn:
        return DetectionEventIn(
            message_id = MessageId(payload['msgId']),
            timestamp = Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            member_id = payload['memberId'],
            type_ = payload['type'],
            product_id = payload['pid'],
            uuid = payload['uuid'],
            mac_address = payload['mac'],
            wpa3 = payload['wpa3'],
            hardware_version = payload['hardwareVersion'],
            software_version = payload['softwareVersion'],
        )

@dataclass
class BindingOut:
    message_id: MessageId
    timestamp: Timestamp
    code: Code
    bind_id: str

    def to_mqtt_payload(self) -> dict:
        return {
            'cmd': Commands.BINDING,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
            'code': self.code.value,
            'bindId': bind_id,
        }

@dataclass
class WifiReconnectServiceOut:
    message_id: MessageId
    timestamp: Timestamp

    @staticmethod
    def create() -> WifiReconnectServiceOut:
        return WifiReconnectServiceOut(
            message_id = MessageId.generate(),
            timestamp = Timestamp.now(),
        )

    def to_mqtt_payload(self) -> dict:
        return {
            'cmd': Commands.WIFI_RECONNECT_SERVICE,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms()
        }

@dataclass
class WifiReconnectServiceIn:
    message_id: MessageId
    timestamp: Timestamp
    code: Code

    @staticmethod
    def from_mqtt_payload(payload: dict) -> WifiReconnectServiceIn:
        return WifiReconnectServiceIn(
            message_id = MessageId(payload['msgId']),
            timestamp = Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            code = Code(int(payload['code'])),
        )

@dataclass
class WifiChangeServiceOut:
    message_id: MessageId
    timestamp: Timestamp
    ssid: str
    password: str

    def to_mqtt_payload(self) -> dict:
        return {
            'cmd': Commands.WIFI_CHANGE_SERVICE,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
            'wifiSsid': self.ssid,
            'password': self.password,
        }

@dataclass
class TutkContractServiceOut:
    message_id: MessageId
    timestamp: Timestamp
    device_tutk_token: str
    device_tutk_url: str
    contract_id: str
    start_time: str
    expires: str

    def to_mqtt_payload(self) -> dict:
        return {
            'cmd': Commands.TUTK_CONTRACT_SERVICE,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
            'deviceTutkToken': self.device_tutk_token,
            'deviceTutkUrl': self.device_tutk_url,
            'contractId': self.contract_id,
            'startTime': self.start_time,
            'expires': self.expires,
        }

@dataclass
class UnbindOut:
    message_id: MessageId
    timestamp: Timestamp
    bind_id: str

    def to_mqtt_payload(self) -> dict:
        return {
            'cmd': Commands.UNBIND,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
            'bindId': self.bind_id,
        }

@dataclass
class ServerConfigPushOut:
    message_id: MessageId
    timestamp: Timestamp
    blocking_time: str

    def to_mqtt_payload(self) -> dict:
        return {
            'cmd': Commands.SERVER_CONFIG_PUSH,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
            'blockingTime': self.blocking_time,
        }

@dataclass
class RestoreOut:
    message_id: MessageId
    timestamp: Timestamp

    @staticmethod
    def create() -> RestoreOut:
        return RestoreOut(
            message_id = MessageId.generate(),
            timestamp = Timestamp.now(),
        )

    def to_mqtt_payload(self) -> dict:
        return {
            'cmd': Commands.RESTORE,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
        }

@dataclass
class RestoreIn:
    message_id: MessageId
    timestamp: Timestamp
    code: Code

    @staticmethod
    def from_mqtt_payload(payload: dict) -> RestoreIn:
        return RestoreIn(
            message_id = MessageId(payload['msgId']),
            timestamp = Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            code = Code(int(payload['code'])),
        )

@dataclass
class InitializeSdCardServiceOut:
    message_id: MessageId
    timestamp: Timestamp

    @staticmethod
    def create() -> InitializeSdCardServiceOut:
        return InitializeSdCardServiceOut(
            message_id = MessageId.generate(),
            timestamp = Timestamp.now(),
        )

    def to_mqtt_payload(self) -> dict:
        return {
            'cmd': Commands.INITIALIZE_SD_CARD_SERVICE,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
        }

@dataclass
class InitializeSdCardServiceIn:
    message_id: MessageId
    timestamp: Timestamp
    code: Code

    @staticmethod
    def from_mqtt_payload(payload: dict) -> InitializeSdCardServiceIn:
        return InitializeSdCardServiceIn(
            message_id = MessageId(payload['msgId']),
            timestamp = Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            code = Code(int(payload['code'])),
        )

@dataclass
class DeviceRebootOut:
    message_id: MessageId
    timestamp: Timestamp

    @staticmethod
    def create() -> DeviceRebootOut:
        return DeviceRebootOut(
            message_id = MessageId.generate(),
            timestamp = Timestamp.now(),
        )

    def to_mqtt_payload(self) -> dict:
        return {
            'cmd': Commands.DEVICE_REBOOT,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
        }

@dataclass
class DeviceRebootIn:
    message_id: MessageId
    timestamp: Timestamp
    code: Code

    @staticmethod
    def from_mqtt_payload(payload: dict) -> DeviceRebootIn:
        return DeviceRebootIn(
            message_id = MessageId(payload['msgId']),
            timestamp = Timestamp.from_timestamp_epoch_ms(int(payload['ts'])),
            code = Code(int(payload['code'])),
        )

@dataclass
class DeviceInfoServiceOut:
    message_id: MessageId
    timestamp: Timestamp
    device_sn: str
    camera_id: str

    def to_mqtt_payload(self) -> dict:
        return {
            'cmd': Commands.DEVICE_INFO_SERVICE,
            'msgId': self.message_id.data,
            'ts': self.timestamp.to_timestamp_epoch_ms(),
            'deviceSn': self.device_sn,
            'cameraId': self.camera_id,
        }

########################################################################################################################

# Client to communicate with the device using the stock firmware protocol over mqtt
class Client:
    def __init__(self, ad: adapi.ADAPI, mqtt: mqttapi.Mqtt, device_serial_number: str):
        self.ad: adapi.ADAPI = ad
        self.mqtt: mqttapi.Mqtt = mqtt
        self.message_topics: MessageTopics = MessageTopics(device_serial_number)

        # heart

        self.heartbeat_callback = None
        
        # ntp

        self.ntp_callback = None
        self.ntp_sync_callback = None

        # ota
        
        self.ota_infom_callback = None
        self.ota_progress_callback = None
        self.ota_upgrade_callback = None

        # service
        
        self.attr_get_service_callback = None
        self.attr_set_service_callback = None
        self.device_feeding_plan_service = None
        self.device_info_service_callback = None
        self.device_properties_service_callback = None
        self.feeding_plan_service_callback = None
        self.initialize_sd_card_service_callback = None
        self.manual_feeding_service_callback = None
        self.tutk_contract_service_callback = None
        self.wifi_change_service_callback = None
        self.wifi_reconnect_service_callback = None

        # event

        self.attr_push_event_callback = None
        self.detection_event_callback = None
        self.device_start_event_callback = None
        self.error_event_callback = None
        self.get_feeding_plan_event_callback = None
        self.grain_output_event_callback = None

        # config

        self.get_config_callback = None
        self.server_config_push_callback = None

        # system

        self.binding_callback = None
        self.device_reboot_callback = None
        self.reset_callback = None
        self.restore_callback = None
        self.unbind_callback = None

    def initialize(self):
        self._mqtt_listen_events(self.message_topics.heart_post_get(), self._mqtt_recv_heart_cb)
        self._mqtt_listen_events(self.message_topics.ota_post_get(), self._mqtt_recv_ota_cb)
        self._mqtt_listen_events(self.message_topics.ntp_post_get(), self._mqtt_recv_ntp_cb)
        self._mqtt_listen_events(self.message_topics.config_post_get(), self._mqtt_recv_config_cb)
        self._mqtt_listen_events(self.message_topics.event_post_get(), self._mqtt_recv_event_cb)
        self._mqtt_listen_events(self.message_topics.service_post_get(), self._mqtt_recv_service_cb)
        self._mqtt_listen_events(self.message_topics.system_post_get(), self._mqtt_recv_system_cb)

    ############################################################################

    ##### heart

    def heartbeat_listen(self, callback):
        self.heartbeat_callback = callback

    ##### ntp

    def ntp_listen(self, callback):
        self.ntp_callback = callback

    def ntp_sync_listen(self, callback):
        self.ntp_sync_callback = callback

    ##### ota

    def ota_inform_listen(self, callback):
        self.ota_inform_callback = callback

    def ota_progress_listen(self, callback):
        self.ota_progress_callback = callback

    def ota_upgrade_listen(self, callback):
        self.ota_upgrade_callback = callback

    ##### service

    def attr_get_service_listen(self, callback):
        self.attr_get_service_callback = callback

    def attr_set_service_listen(self, callback):
        self.attr_set_service_callback = callback

    def device_feeding_plan_service_listen(self, callback):
        self.device_feeding_plan_service_callback = callback

    def device_info_service_listen(self, callback):
        self.device_info_service_callback = callback

    def device_properties_service_listen(self, callback):
        self.device_properties_service_callback = callback

    def feeding_plan_service_listen(self, callback):
        self.feeding_plan_service_callback = callback
    
    def initialize_sd_card_service_listen(self, callback):
        self.initialize_sd_card_service_callback = callback

    def manual_feeding_service_listen(self, callback):
        self.manual_feeding_service_callback = callback

    def tutk_contract_service_listen(self, callback):
        self.tutk_contract_service_callback = callback

    def wifi_change_service_listen(self, callback):
        self.wifi_change_service_callback = callback

    def wifi_reconnect_service_listen(self, callback):
        self.wifi_reconnect_service_callback = callback

    ##### event

    def attr_push_event_listen(self, callback):
        self.attr_push_event_callback = callback

    def detection_event_listen(self, callback):
        self.detection_event_callback = callback

    def device_start_event_listen(self, callback):
        self.device_start_event_callback = callback

    def error_event_listen(self, callback):
        self.error_event_callback = callback

    def get_feeding_plan_event_listen(self, callback):
        self.get_feeding_plan_event_callback = callback

    def grain_output_event_listen(self, callback):
        self.grain_output_event_callback = callback

    ##### config

    def get_config_listen(self, callback):
        self.get_config_callback = callback

    def server_config_push_listen(self, callback):
        self.server_config_push_callback = callback

    ##### system

    def binding_listen(self, callback):
        self.binding_callback = callback

    def device_reboot_listen(self, callback):
        self.device_reboot_callback = callback

    def reset_listen(self, callback):
        self.reset_callback = callback
    
    def restore_listen(self, callback):
        self.restore_callback = callback

    def unbind_listen(self, callback):
        self.unbind_callback = callback

    ############################################################################

    ##### ntp

    def ntp_send(self, ntp_out: NtpOut):
        self._mqtt_ntp_send(ntp_out.to_mqtt_payload())

    def ntp_sync_send(self, ntp_sync_out: NtpSyncOut):
        self._mqtt_ntp_send(ntp_sync_out.to_mqtt_payload())

    ##### ota

    def ota_inform_send(self, ota_inform_out: OtaInformOut):
        self._mqtt_ota_send(ota_inform_out.to_mqtt_payload())

    def ota_progress_send(self, ota_progress_out: OtaProgressOut):
        self._mqtt_ota_send(ota_progress_out.to_mqtt_payload())

    def ota_upgrade_send(self, ota_upgrade_out: OtaUpgradeOut):
        self._mqtt_ota_send(ota_upgrade_out.to_mqtt_payload())

    ##### service

    def attr_get_service_send(self, attr_get_service_out: AttrGetServiceOut):
        self._mqtt_service_send(attr_get_service_out.to_mqtt_payload())

    def attr_set_service_send(self, attr_set_service_out: AttrSetServiceOut):
        self._mqtt_service_send(attr_set_service_out.to_mqtt_payload())

    def device_feeding_plan_service_send(self, device_feeding_plan_service_out: DeviceFeedingPlanServiceOut):
        self._mqtt_service_send(device_feeding_plan_service_out.to_mqtt_payload())

    def device_info_service_send(self, device_info_service_out: DeviceInfoServiceOut):
        self._mqtt_service_send(device_info_service_out.to_mqtt_payload())

    def device_properties_service_send(self, device_properties_service_out: DevicePropertiesServiceOut):
        self._mqtt_service_send(device_properties_service_out.to_mqtt_payload())

    def feeding_plan_service_send(self, feeding_plan_service_out: FeedingPlanServiceOut):
        self._mqtt_service_send(feeding_plan_service_out.to_mqtt_payload())

    def initialize_sd_card_service_send(self, initialize_sd_card_service_out: InitializeSdCardServiceOut):
        self._mqtt_service_send(initialize_sd_card_service_out.to_mqtt_payload())

    def manual_feeding_service_send(self, manual_feeding_service_out: ManualFeedingServiceOut):
        self._mqtt_service_send(manual_feeding_service_out.to_mqtt_payload())

    def tutk_contract_service_send(self, tutk_contract_service_out: TutkContractServiceOut):
        self._mqtt_service_send(tutk_contract_service_out.to_mqtt_payload())

    def wifi_change_service_send(self, wifi_change_service_out: WifiChangeServiceOut):
        self._mqtt_service_send(wifi_change_service_out.to_mqtt_payload())

    def wifi_reconnect_service_send(self, wifi_reconnect_service_out: WifiReconnectServiceOut):
        self._mqtt_service_send(wifi_reconnect_service_out.to_mqtt_payload())

    ##### event

    def attr_push_event_send(self, attr_push_event_out: AttrPushEventOut):
        self._mqtt_event_send(attr_push_event_out.to_mqtt_payload())

    def device_start_event_send(self, device_start_event_out: DeviceStartEventOut):
        self._mqtt_event_send(device_start_event_out.to_mqtt_payload())

    def error_event_send(self, error_event_out: ErrorEventOut):
        self._mqtt_event_send(error_event_out.to_mqtt_payload())

    def get_feeding_plan_event_send(self, get_feeding_plan_event_out: GetFeedingPlanEventOut):
        self._mqtt_service_send(get_feeding_plan_event_out.to_mqtt_payload())

    def grain_output_event_send(self, grain_output_event_out: GrainOutputEventOut):
        self._mqtt_service_send(grain_output_event_out.to_mqtt_payload())

    ##### config

    def get_config_send(self, get_config_out: GetConfigOut):
        self._mqtt_config_send(get_config_out.to_mqtt_payload())

    def server_config_push_send(self, server_config_push_out: ServerConfigPushOut):
        self._mqtt_config_send(server_config_push_send.to_mqtt_payload())

    ##### system

    def binding_send(self, binding_out: BindingOut):
        self._mqtt_system_send(binding_out.to_mqtt_payload())

    def device_reboot_send(self, device_reboot_out: DeviceRebootOut):
        self._mqtt_system_send(device_reboot_out.to_mqtt_payload())

    def reset_send(self, reset_out: ResetOut):
        self._mqtt_system_send(reset_out.to_mqtt_payload())
    
    def restore_send(self, restore_out: RestoreOut):
        self._mqtt_system_send(restore_out.to_mqtt_payload())
    
    def unbind(self, unbind_out: UnbindOut):
        self._mqtt_system_send(unbind_out.to_mqtt_payload())

    ############################################################################

    def _mqtt_recv_heart_cb(self, eventname: str, data: dict, kwargs):
        self.ad.log(data)

        payload: dict = json.loads(data['payload'])
        cmd: str = payload['cmd']

        if cmd == Commands.HEARTBEAT:
            if not self.heartbeat_callback == None:
                heartbeat_in = HeartbeatIn.from_mqtt_payload(payload)
                self.heartbeat_callback(heartbeat_in)
        else:
            self.ad.error("Unknown cmd {} on heartbeat receive: {}".format(cmd, payload))

    def _mqtt_recv_ntp_cb(self, eventname: str, data: dict, kwargs):
        self.ad.log(data)

        payload: dict = json.loads(data['payload'])
        cmd: str = payload['cmd']

        if cmd == Commands.NTP:
            if not self.ntp_callback == None:
                ntp_int = NtpIn.from_mqtt_payload(payload)
                self.ntp_callback(ntp_int)
        elif cmd == Commands.NTP_SYNC:
            if not self.ntp_sync_callback == None:
                ntp_sync_in = NtpSyncIn.from_mqtt_payload(payload)
                self.ntp_sync_callback(ntp_sync_in)
        else:
            self.ad.error("Unknown cmd {} on ntp receive: {}".format(cmd, payload))

    def _mqtt_recv_ota_cb(self, eventname: str, data: dict, kwargs):
        self.ad.log(data)

        payload: dict = json.loads(data['payload'])
        cmd: str = payload['cmd']

        if cmd == Commands.OTA_INFORM:
            if not self.ota_inform_callback == None:
                ota_inform_in = OtaInformIn.from_mqtt_payload(payload)
                self.ota_inform_callback(ota_inform_in)
        elif cmd == Commands.OTA_PROGRESS:
            if not self.ota_progress_callback == None:
                ota_progress_in = OtaProgressIn.from_mqtt_payload(payload)
                self.ota_progress_callback(ota_progress_in)
        elif cmd == Commands.OTA_UPGRADE:
            if not self.ota_upgrade_callback == None:
                ota_upgrade_in = OtaUpgradeIn.from_mqtt_payload(payload)
                self.ota_upgrade_callback(ota_upgrade_in)
        else:
            self.ad.error("Unknown cmd {} on ota receive: {}".format(cmd, payload))

    def _mqtt_recv_service_cb(self, eventname: str, data: dict, kwargs):
        self.ad.log(data)

        payload: dict = json.loads(data['payload'])
        cmd: str = payload['cmd']

        if cmd == Commands.ATTR_SET_SERVICE:
            if not self.attr_set_service_callback == None:
                attr_set_service_in = AttrSetServiceIn.from_mqtt_payload(payload)
                self.attr_set_service_callback(attr_set_service_in)
        elif cmd == Commands.DEVICE_FEEDING_PLAN_SERVICE:
            if not self.device_feeding_plan_service_callback == None:
                device_feeding_plan_service_in = DeviceFeedingPlanServiceIn.from_mqtt_payload(payload)
                self.device_feeding_plan_service_callback(device_feeding_plan_service_in)
        elif cmd == Commands.DEVICE_INFO_SERVICE:
            if not self.device_info_service_callback == None:
                device_info_service_in = DeviceInfoServiceIn.from_mqtt_payload(payload)
                self.device_info_service_callback(device_info_service_in)
        elif cmd == Commands.DEVICE_PROPERTIES_SERVICE:
            if not self.device_properties_service_callback == None:
                device_properties_service_in = DevicePropertiesServiceIn.from_mqtt_payload(payload)
                self.device_properties_service_callback(device_properties_service_in)
        elif cmd == Commands.DEVICE_REBOOT:
            if not self.device_reboot_callback == None:
                device_reboot_in = DeviceRebootIn.from_mqtt_payload(payload)
                self.device_reboot_callback(device_reboot_in)
        elif cmd == Commands.FEEDING_PLAN_SERVICE:
            if not self.feeding_plan_service_callback == None:
                feeding_plan_service_in = FeedingPlanServiceIn.from_pqtt_payload(payload)
                self.feeding_plan_service_callback(feeding_plan_service_in)
        elif cmd == Commands.INITIALIZE_SD_CARD_SERVICE:
            if not self.initialize_sd_card_service_callback == None:
                initialize_sd_card_service_in = InitializeSdCardServiceIn.from_mqtt_payload(payload)
                self.initialize_sd_card_service_callback(initialize_sd_card_service_in)
        elif cmd == Commands.MANUAL_FEEDING_SERVICE:
            if not self.manual_feeding_service_callback == None:
                manual_feeding_service_in = ManualFeedingServiceIn.from_mqtt_payload(payload)
                self.manual_feeding_service_callback(manual_feeding_service_in)
        elif cmd == Commands.TUTK_CONTRACT_SERVICE:
            if not self.tutk_contract_service_callback == None:
                tutk_contract_service_in = TutkContractServiceIn.from_mqtt_payload(payload)
                self.tutk_contract_service_callback(tutk_contract_service_in)
        elif cmd == Commands.WIFI_CHANGE_SERVICE:
            if not self.wifi_change_service_callback == None:
                wifi_change_service_in = WifiChangeServiceIn.from_mqtt_payload(payload)
                self.wifi_change_service_callback(wifi_change_service_in)
        elif cmd == Commands.WIFI_RECONNECT_SERVICE:
            if not self.wifi_reconnect_service_callback == None:
                wifi_reconnect_service_in = WifiReconnectServiceIn.from_mqtt_payload(payload)
                self.wifi_reconnect_service_callback(wifi_reconnect_service_in)   
        else:
            self.ad.error("Unknown cmd {} on service receive: {}".format(cmd, payload))

    def _mqtt_recv_event_cb(self, eventname: str, data: dict, kwargs):
        self.ad.log(data)

        payload: dict = json.loads(data['payload'])
        cmd: str = payload['cmd']

        # This service call is on the event "channel" for some reason :/
        if cmd == Commands.ATTR_GET_SERVICE:
            if not self.attr_get_service_callback == None:
                attr_get_service_in = AttrGetServiceIn.from_mqtt_payload(payload)
                self.attr_get_service_callback(attr_get_service_in)
        elif cmd == Commands.ATTR_PUSH_EVENT:
            if not self.attr_push_event_callback == None:
                attr_push_event_in = AttrPushEventIn.from_mqtt_payload(payload)
                self.attr_push_event_callback(attr_push_event_in)
        elif cmd == Commands.DETECTION_EVENT:
            if not self.detection_event_callback == None:
                detection_event_in = DetectionEventIn.from_mqtt_payload(payload)
                self.detection_event_callback(detection_event_in)
        elif cmd == Commands.DEVICE_START_EVENT:
            if not self.device_start_event_callback == None:
                device_start_event_in = DeviceStartEventIn.from_mqtt_payload(payload)
                self.device_start_event_callback(device_start_event_in)
        elif cmd == Commands.ERROR_EVENT:
            if not self.error_event_callback == None:
                error_event_in = ErrorEventIn.from_mqtt_payload(payload)
                self.error_event_callback(error_event_in)
        elif cmd == Commands.GET_FEEDING_PLAN_EVENT:
            if not self.get_feeding_plan_event_callback == None:
                get_feeding_plan_event_in = GetFeedingPlanEventIn.from_mqtt_payload(payload)
                self.get_feeding_plan_event_callback(get_feeding_plan_event_in)
        elif cmd == Commands.GRAIN_OUTPUT_EVENT:
            if not self.grain_output_event_callback == None:
                grain_output_event_in = GrainOutputEventIn.from_mqtt_payload(payload)
                self.grain_output_event_callback(grain_output_event_in)
        else:
            self.ad.error("Unknown cmd {} on event receive: {}".format(cmd, payload))

    def _mqtt_recv_config_cb(self, eventname: str, data: dict, kwargs):
        self.ad.log(data)

        payload: dict = json.loads(data['payload'])
        cmd: str = payload['cmd']

        if cmd == Commands.GET_CONFIG:
            if not self.get_config_callback == None:
                get_config_in = GetConfigIn.from_mqtt_payload(payload)
                self.get_config_callback(get_config_in)
        elif cmd == Commands.SERVER_CONFIG_PUSH:
            if not self.server_config_push_callback == None:
                server_config_push_in = ServerConfigPushIn.from_mqtt_payload(payload)
                self.server_config_push_callback(server_config_push_in)
        else:
            self.ad.error("Unknown cmd {} on config receive: {}".format(cmd, payload))

    def _mqtt_recv_system_cb(self, eventname: str, data: dict, kwargs):
        self.ad.log(data)

        payload: dict = json.loads(data['payload'])
        cmd: str = payload['cmd']

        if cmd == Commands.BINDING:
            if not self.binding_callback == None:
                binding_in = BindingIn.from_mqtt_payload(payload)
                self.binding_callback(binding_in)
        elif cmd == Commands.DEVICE_REBOOT:
            if not self.device_reboot_callback == None:
                device_reboot_in = DeviceRebootIn.from_mqtt_payload(payload)
                self.device_reboot_callback(device_reboot_in)
        elif cmd == Commands.RESET:
            if not self.reset_callback == None:
                reset_in = ResetIn.from_mqtt_payload(payload)
                self.reset_callback(reset_in)
        elif cmd == Commands.RESTORE:
            if not self.restore_callback == None:
                restore_in = RestoreIn.from_mqtt_payload(payload)
                self.restore_callback(restore_in)
        elif cmd == Commands.UNBIND:
            if not self.unbind_callback == None:
                unbind_in = UnbindIn.from_mqtt_payload(payload)
                self.unbind_callback(unbind_in)
        else:
            self.ad.error("Unknown cmd {} on system receive: {}".format(cmd, payload))

    ############################################################################

    def _mqtt_ota_send(self, payload: dict):
        self._mqtt_send(self.message_topics.ota_sub_get(), payload)

    def _mqtt_ntp_send(self, payload: dict):
        self._mqtt_send(self.message_topics.ntp_sub_get(), payload)

    def _mqtt_broadcast_send(self, payload: dict):
        self._mqtt_send(self.message_topics.broadcast_sub_get(), payload)

    def _mqtt_config_send(self, payload: dict):
        self._mqtt_send(self.message_topics.config_sub_get(), payload)

    def _mqtt_event_send(self, payload: dict):
        self._mqtt_send(self.message_topics.event_sub_get(), payload)

    def _mqtt_service_send(self, payload: dict):
        self._mqtt_send(self.message_topics.service_sub_get(), payload)

    def _mqtt_system_send(self, payload: dict):
        self._mqtt_send(self.message_topics.system_sub_get(), payload)

    def _mqtt_listen_events(self, topic: str, callback):
        self.mqtt.listen_event(callback, "MQTT_MESSAGE", topic = topic, namespace = 'mqtt')

    def _mqtt_send(self, topic: str, payload: dict):
        payload_json: str = json.dumps(payload)

        self.ad.log("{}: {}".format(topic, payload_json))

        self.mqtt.mqtt_publish(topic, payload_json, namespace = "mqtt")

########################################################################################################################

class Watchdog:
    def __init__(self, ad: adapi.ADAPI, name: str, period_sec: int):
        self.ad: adapi.ADAPI = ad
        self.name = name
        self.period_sec = period_sec

        self.handle = None
        self.trigger_callback = None

    def trigger_listen(self, callback):
        self.trigger_callback = callback

    def reset(self):
        self.ad.log("[{}] watchdog reset".format(self.name))

        self._cancel()
        self._schedule()

    def cancel(self):
        self._cancel()

    def _schedule(self):
        self.handle = self.ad.run_in(self._watchdog_run, self.period_sec)

    def _watchdog_run(self, cb_args):
        self.handle = None

        self.ad.log("[{}] watchdog triggered".format(self.name))

        if not self.trigger_callback == None:
            self.trigger_callback()

    def _cancel(self):
        self.ad.cancel_timer(self.handle, True)
        self.handle = None

########################################################################################################################

@dataclass
class FoodPlan:
    id_: int
    execution_time: HourMinTimestamp
    scheduled_days: WeekdaySchedule
    enable_audio: bool
    play_audio_times: int
    grain_num: int

    def set(self, other: FoodPlan):
        self.id_ = other.id_
        self.execution_time = other.execution_time
        self.scheduled_days = other.scheduled_days
        self.enable_audio = other.enable_audio
        self.play_audio_times = other.play_audio_times
        self.grain_num = other.grain_num

    @staticmethod
    def from_dict(data: dict) -> FoodPlan:
        return FoodPlan(
            id_ = int(data['id']),
            execution_time = HourMinTimestamp.from_dict(data['execution_time']),
            scheduled_days = WeekdaySchedule.from_list(data['scheduled_days']),
            enable_audio = bool(data['enable_audio']),
            play_audio_times = int(data['play_audio_times']),
            grain_num = int(data['grain_num'])
        )

    def to_dict(self) -> dict:
        return {
            'id': self.id_,
            'execution_time': self.execution_time.to_dict(),
            'scheduled_days': self.scheduled_days.to_list(),
            'enable_audio': self.enable_audio,
            'play_audio_times': self.play_audio_times,
            'grain_num': self.grain_num
        }

@dataclass
class FoodPlans:
    plans: [FoodPlan]

    @staticmethod
    def create_empty() -> FoodPlans:
        return FoodPlans([])

    @staticmethod
    def create(*args: FoodPlan) -> FoodPlans:
        return FoodPlans(args)

    def plan_set(self, food_plan: FoodPlan):
        plan_found: bool = False

        for plan in self.plans:
            if plan.id_ == food_plan.id_:
                plan_found = True

                plan.set(food_plan)
                break

        if plan_found == False:
            self.plans.append(food_plan)

    @staticmethod
    def from_dict(data: dict) -> FoodPlans:
        plans = data['plans']

        food_plans = []

        for plan in plans:
            food_plans.append(FoodPlan.from_dict(plan))

        return FoodPlans(food_plans)

    def to_dict(self) -> dict:
        plans = []

        for plan in self.plans:
            plans.append(plan.to_dict())

        return {
            'plans': plans
        }


class FoodOutputProgress(enum.Enum):
    IDLE = 0
    RUNNING = 1
    BLOCKED = 2
    ERROR = 3

# Backend for life cycle handling and providing a more streamlined interface
# to interact with the remote device/client
class Backend:
    # Apparently every 51 seconds, give it some buffer
    HEARTBEAT_WATCHDOG_PERIOD_SEC: int = 51 + 30
    DEVICE_INIT_WATCHDOG_PERIOD_SEC: int = 10
    NTP_SYNC_TIME_DIFF_THRESHOLD_SEC: int = 10

    def initialize(self, ad: adapi.ADAPI, mqtt: mqttapi.Mqtt, device_serial: str, mqtt_host: str, mqtt_port: int, food_plans: FoodPlans):
        self.mqtt_host: str = mqtt_host
        self.mqtt_port: int = mqtt_port

        self.food_plans: FoodPlans = food_plans

        self.ad: adapi.ADAPI = ad

        self.device_serial = device_serial

        self.client = Client(ad, mqtt, device_serial)

        self.client.heartbeat_listen(self._heartbeat_cb)

        self.client.ntp_listen(self._ntp_cb)
        self.client.ntp_sync_listen(self._ntp_sync_cb)
        
        self.client.device_start_event_listen(self._device_start_event_cb)
        self.client.device_reboot_listen(self._device_reboot_cb)
        self.client.restore_listen(self._restore_cb)
        self.client.initialize_sd_card_service_listen(self._initialize_sd_card_service_cb)
        self.client.wifi_reconnect_service_listen(self._wifi_reconnect_service_cb)
        
        self.client.attr_get_service_listen(self._attr_get_service_cb)
        self.client.attr_push_event_listen(self._attr_push_event_cb)
        self.client.attr_set_service_listen(self._attr_set_service_cb)
        self.client.get_config_listen(self._get_config_cb)

        self.client.get_feeding_plan_event_listen(self._get_feeding_plan_event_cb)
        
        self.client.manual_feeding_service_listen(self._manual_feeding_service_cb)
        self.client.feeding_plan_service_listen(self._feeding_plan_service_cb)
        self.client.grain_output_event_listen(self._grain_output_event_cb)

        # Determine when the device is considered offline. The device sends a periodic
        # heartbeat message that is used to reset the watchdog
        self.heartbeat_watchdog = Watchdog(ad, 'Heartbeat', Backend.HEARTBEAT_WATCHDOG_PERIOD_SEC)
        # Ensure this is always reset, will fire if not coming (back) online after a restart
        # to update the state
        self.heartbeat_watchdog.reset()
        self.heartbeat_watchdog.trigger_listen(self._heartbeat_watchdog_trigger)

        self.went_online_callback = None
        self.went_offline_callback = None
        self.ntp_sync_status_callback = None
        self.error_callback = None

        self.device_info_callback = None
        self.device_wifi_info_callback = None
        self.device_sd_card_info_callback = None

        self.settings_audio_callback = None
        self.settings_camera_callback = None
        self.settings_recording_callback = None
        self.settings_motion_detection_callback = None
        self.settings_sound_detection_callback = None
        self.settings_cloud_video_recording_callback = None
        self.settings_sound_callback = None
        self.settings_button_lights_callback = None
        self.settings_feeding_video_callback = None
        self.settings_buttons_auto_lock_callback = None

        self.state_power_callback = None
        self.state_food_callback = None

        self.food_output_log_start_callback = None
        self.food_output_log_end_callback = None
        self.food_output_progress_callback = None

        self.last_heartbeat_count: int = 0
        self.is_online: bool = False
        self.ntp_sync_error: bool = False

        # Hack:
        # These are actually stored on the device but the device
        # always requires both to be sent in any message if only one is changed
        # This is not applicable to any other settings, so keep a temporary
        # state of these, i.e. cache them
        self.settings_audio_enabled: bool = False
        self.settings_audio_file_url: str = ""

        self.client.initialize()

    ###########################################################################

    def went_online_listen(self, callback):
        self.went_online_callback = callback

    def went_offline_listen(self, callback):
        self.went_offline_callback = callback

    def ntp_sync_status_listen(self, callback):
        self.ntp_sync_status_callback = callback

    def error_listen(self, callback):
        self.error_callback = callback

    def device_info_listen(self, callback):
        self.device_info_callback = callback

    def device_wifi_info_listen(self, callback):
        self.device_wifi_info_callback = callback

    def device_sd_card_info_listen(self, callback):
        self.device_sd_card_info_callback = callback

    def settings_audio_listen(self, callback):
        self.settings_audio_callback = callback

    def settings_camera_listen(self, callback):
        self.settings_camera_callback = callback

    def settings_recording_listen(self, callback):
        self.settings_recording_callback = callback

    def settings_motion_detection_listen(self, callback):
        self.settings_motion_detection_callback = callback

    def settings_sound_detection_listen(self, callback):
        self.settings_sound_detection_callback = callback

    def settings_cloud_video_recording_listen(self, callback):
        self.settings_cloud_video_recording_callback = callback

    def settings_sound_listen(self, callback):
        self.settings_sound_callback = callback

    def settings_button_lights_listen(self, callback):
        self.settings_button_lights_callback = callback

    def settings_feeding_video_listen(self, callback):
        self.settings_feeding_video_callback = callback

    def settings_buttons_auto_lock_listen(self, callback):
        self.settings_buttons_auto_lock_callback = callback

    def state_power_listen(self, callback):
        self.state_power_callback = callback

    def state_food_listen(self, callback):
        self.state_food_callback = callback

    def food_output_log_start_listen(self, callback):
        self.food_output_log_start_callback = callback

    def food_output_log_end_listen(self, callback):
        self.food_output_log_end_callback = callback

    def food_output_progress_listen(self, callback):
        self.food_output_progress_callback = callback

    ###########################################################################

    def settings_audio(self, enable: bool = None, file_url: str = None):
        attr_set_service_out = AttrSetServiceOut.create(
            enable_audio = enable if not enable == None else self.settings_audio_enabled,
            audio_url = file_url if not file_url == None else self.settings_audio_file_url,
        )

        if not enable == None:
            self.settings_audio_enabled = enable
        if not file_url == None:
            self.settings_audio_file_url = file_url

        self.client.attr_set_service_send(attr_set_service_out)

    def settings_camera(self, enable: bool = None, aging_type: AgingType = None, night_vision: NightVision = None, resolution: Resolution = None):
        attr_set_service_out = AttrSetServiceOut.create(
            camera_switch = enable,
            camera_aging_type = aging_type,
            night_vision = night_vision,
            resolution = resolution
        )
        self.client.attr_set_service_send(attr_set_service_out)

    def settings_recording(self, enable: bool = None, aging_type: AgingType = None, mode: VideoRecordMode = None):
        attr_set_service_out = AttrSetServiceOut.create(
            video_record_switch = enable,
            video_record_aging_type = aging_type,
            video_record_mode = mode,
        )
        self.client.attr_set_service_send(attr_set_service_out)

    def settings_sound(self, enable: bool = None, aging_type: AgingType = None, volume: PercentageInt = None):
        attr_set_service_out = AttrSetServiceOut.create(
            sound_switch = enable,
            sound_aging_type = aging_type,
            volume = volume
        )
        self.client.attr_set_service_send(attr_set_service_out)

    def settings_motion_detection(self, enable: bool = None, aging_type: AgingType = None, range_: MotionDetectionRange = None, sensitivity: MotionDetectionSensitivity = None):
        attr_set_service_out = AttrSetServiceOut.create(
            motion_detection_switch = enable,
            motion_detection_aging_type = aging_type,
            motion_detection_range = range_,
            motion_detection_sensitivity = sensitivity,
        )
        self.client.attr_set_service_send(attr_set_service_out)

    def settings_sound_detection(self, enable: bool = None, aging_type: AgingType = None, sensitivity: SoundDetectionSensitivity = None):
        attr_set_service_out = AttrSetServiceOut.create(
            sound_detection_switch = enable,
            sound_detection_aging_type = aging_type,
            sound_detection_sensitivity = sensitivity,
        )
        self.client.attr_set_service_send(attr_set_service_out)

    def settings_cloud_video_recording(self, enable: bool = None):
        attr_set_service_out = AttrSetServiceOut.create(
            cloud_video_record_switch = enable,
        )
        self.client.attr_set_service_send(attr_set_service_out)

    def settings_button_lights(self, enable: bool = None, aging_type: AgingType = None):
        attr_set_service_out = AttrSetServiceOut.create(
            light_switch = enable,
            light_aging_type = aging_type,
        )
        self.client.attr_set_service_send(attr_set_service_out)

    def settings_buttons_auto_lock(self, enable: bool = None, threshold: int = None):
        attr_set_service_out = AttrSetServiceOut.create(
            auto_change_mode = enable,
            auto_threshold = threshold,
        )
        self.client.attr_set_service_send(attr_set_service_out)

    def settings_feeding_video(self, enable: bool = None, video_on_start_feeding_plan: bool = None, video_after_manual_feeding: bool = None, recording_length_before_feeding_plan_time: int = None, recording_length_after_manual_feeding_time: int = None, video_watermark: bool = None, automatic_recording: int = None):
        attr_set_service_out = AttrSetServiceOut.create(
            feeding_video_switch = enable,
            enable_video_start_feeding_plan = video_on_start_feeding_plan,
            enable_video_after_manual_feeding = video_after_manual_feeding,
            before_feeding_plan_time = recording_length_before_feeding_plan_time,
            automatic_recording = automatic_recording,
            after_manual_feeding_time = recording_length_after_manual_feeding_time,
            video_watermark_switch = video_watermark,
        )
        self.client.attr_set_service_send(attr_set_service_out)

    def food_plans_set(self, food_plans: FoodPlans):
        # Update internal stored food plans
        # Required to hold this if the device re-connects and asks
        # for syncing the current food plans
        self.food_plans = food_plans

        self._device_food_plans_sync(self.food_plans)

    def food_manual_feed_now(self, grain_num: int):
        manual_feeding_service_out = ManualFeedingServiceOut.create(grain_num = grain_num)
        self.client.manual_feeding_service_send(manual_feeding_service_out)

    def device_reboot(self):
        device_reboot_out = DeviceRebootOut.create()
        self.client.device_reboot_send(device_reboot_out)

    def device_factory_reset(self):
        restore_out = RestoreOut.create()
        self.client.restore_send(restore_out)

    def device_wifi_reconnect(self):
        wifi_reconnect_service_out = WifiReconnectServiceOut.create()
        self.client.wifi_reconnect_service_send(wifi_reconnect_service_out)

    def device_sd_card_format(self):
        initialize_sd_card_service_out = InitializeSdCardServiceOut.create()
        self.client.initialize_sd_card_service_send(initialize_sd_card_service_out)

    ###########################################################################

    def _heartbeat_cb(self, heartbeat_in: HeartbeatIn):
        # Check if device restarted which might have not been detected by the watchdog
        # between two heartbeat messages
        if heartbeat_in.count < self.last_heartbeat_count:
            self.is_online == False

            if not self.went_offline_callback == None:
                self.went_offline_callback()

        self.last_heartbeat_count = heartbeat_in.count

        if self.is_online == False:
            # Steps to bring the device "online"

            # Full state sync 
            # Two cases:
            # - Device got rebooted and needs to be re-initialized
            # - Integration/backend got restarted and the device is already initialized and doing fine
            # The following covers for both cases though a re-init will also trigger the
            # DEVICE_START_EVENT. In any case, this ensures that the backend will always sync all
            # device state before considering the device online
            get_config_out = GetConfigOut.create()
            self.client.get_config_send(get_config_out)

            attr_get_service_out = AttrGetServiceOut.create()
            self.client.attr_get_service_send(attr_get_service_out)

            # Re-sync food plans
            self._device_food_plans_sync(self.food_plans)

            self.is_online = True

            if not self.went_online_callback == None:
                self.went_online_callback()

            if not self.device_info_callback == None:
                self.device_info_callback(device_serial = self.device_serial)

            if not self.device_wifi_info_callback == None:
                self.device_wifi_info_callback(rssi = heartbeat_in.rssi, type_ = heartbeat_in.wifi_type)

            # Force NTP sync because we don't know when that happened the last time to ensure that
            # the feeding plans are executed correctly
            timestamp_now = Timestamp.now()

            if not self._device_timestamp_sync_drift_check(timestamp_now, heartbeat_in.timestamp):
                self.ad.error("Device NTP sync not successful, timestamp local {} <-> timestamp device {}".format(timestamp_now, ntp_sync_in.timestamp))
                
                if not self.ntp_sync_status_callback == None:
                    self.ntp_sync_status_callback(False)
            else:
                if not self.ntp_sync_status_callback == None:
                    self.ntp_sync_status_callback(True)
        else:
            # With every heartbeat, re-sync the device state
            # Somewhat duct-taping inconsistent state issues
            # as I have seen odd things happening like the food level state
            # or the food output blocked state not being updated properly
            # via the push events
            attr_get_service_out = AttrGetServiceOut.create()
            self.client.attr_get_service_send(attr_get_service_out)

        # Periodic heartbeat resets the watchdog as long as the device keeps responding
        self.heartbeat_watchdog.reset()

    def _ntp_cb(self, ntp_in: NtpIn):
        timestamp_now = Timestamp.now()
        force_time_calbiration = None

        # Initial NTP package is also a check if the device has to re-sync the time
        # Don't consider this an error, yet
        if not self._device_timestamp_sync_drift_check(timestamp_now, ntp_in.timestamp):
            self.ad.log("Device NTP time drift detected, forcing time calibration on device")

            force_time_calbiration = True
        else:
            force_time_calbiration = False

        ntp_out = NtpOut(
            code = Code.OK, 
            timestamp = timestamp_now,
            calibration_tag = force_time_calbiration)

        self.client.ntp_send(ntp_out)

        if not self.ntp_sync_status_callback == None:
            self.ntp_sync_status_callback(True)

    def _ntp_sync_cb(self, ntp_sync_in: NtpSyncIn):
        timestamp_now = Timestamp.now()

        # Basically just an ack from the device, but check again that drift is actually fine
        if not self._device_timestamp_sync_drift_check(timestamp_now, ntp_sync_in.timestamp):
            self.ad.error("Device NTP sync not successful, timestamp local {} <-> timestamp device {}".format(timestamp_now, ntp_sync_in.timestamp))
            
            if not self.ntp_sync_status_callback == None:
                self.ntp_sync_status_callback(False)
        else:
            if not self.ntp_sync_status_callback == None:
                self.ntp_sync_status_callback(True)

    def _device_start_event_cb(self, device_start_event: DeviceStartEventIn):
        if device_start_event.success == True:
            if not self.device_info_callback == None:
                self.device_info_callback(
                    product_id = device_start_event.pid,
                    uuid = device_start_event.uuid,
                    hardware_version = device_start_event.hardware_version,
                    software_version = device_start_event.software_version)

            if not self.device_wifi_info_callback == None:
                self.device_wifi_info_callback(mac_address = device_start_event.mac)
        else:
            _error_report("Device initialization failed")

        device_start_event_out = DeviceStartEventOut.create(
            message_id = device_start_event.message_id,
            code = Code.OK
        )
        self.client.device_start_event_send(device_start_event_out)

        self._device_timestamp_sync_drift_check_and_adjust(device_start_event.timestamp)

    def _device_reboot_cb(self, device_reboot_in: DeviceRebootIn):
        if device_reboot_in.code != Code.OK:
            _error_report("Rebooting failed")
            return

        self.is_online = False

        if not self.went_offline_callback == None:
            self.went_offline_callback()

        # No need for sync drift check on device reboot

    def _restore_cb(self, restore_in: RestoreIn):
        if restore_in.code != Code.OK:
            _error_report("Factory reset failed")
            return

        self.is_online = False

        if not self.went_offline_callback == None:
            self.went_offline_callback()

        # No need for sync drift check on device restore
    
    def _initialize_sd_card_service_cb(self, initialize_sd_card_service_in: InitializeSdCardServiceIn):
        if initialize_sd_card_service_in.code != Code.OK:
            _error_report("Formatting SD card failed")
            return

        self._device_timestamp_sync_drift_check_and_adjust(initialize_sd_card_service_in.timestamp)

    def _wifi_reconnect_service_cb(self, wifi_reconnect_service_in: WifiReconnectServiceIn):
        if wifi_reconnect_service_in.code != Code.OK:
            _error_report("Wifi force reconnect failed")
            return

        self.is_online = False

        if not self.went_offline_callback == None:
            self.went_offline_callback()

        # No need for sync drift check, reconnect triggers NTP call again

    def _attr_get_service_cb(self, attr_get_service_in: AttrGetServiceIn):
        if not self.settings_audio_callback == None:
            self.settings_audio_callback(
                enable = attr_get_service_in.enable_audio,
                url = attr_get_service_in.audio_url,
            )

        # Update cached values
        if not attr_get_service_in.enable_audio == None:
            self.settings_audio_enabled = attr_get_service_in.enable_audio
        if not attr_get_service_in.audio_url == None:
            self.settings_audio_file_url = attr_get_service_in.audio_url

        if not self.settings_camera_callback == None:
            self.settings_camera_callback(
                feature_enabled = attr_get_service_in.enable_camera,
                enable = attr_get_service_in.camera_switch,
                aging_type = attr_get_service_in.camera_aging_type,
                night_vision = attr_get_service_in.night_vision,
                resolution = attr_get_service_in.resolution,
            )

        if not self.settings_recording_callback == None:
            self.settings_recording_callback(
                feature_enabled = attr_get_service_in.enable_video_record,
                enable = attr_get_service_in.video_record_switch,
                aging_type = attr_get_service_in.video_record_aging_type,
                mode = attr_get_service_in.video_record_mode
            )

        if not self.settings_motion_detection_callback == None:
            self.settings_motion_detection_callback(
                feature_enabled = attr_get_service_in.enable_motion_detection,
                enable = attr_get_service_in.motion_detection_switch,
                aging_type = attr_get_service_in.motion_detection_aging_type,
                range_ = attr_get_service_in.motion_detection_range,
                sensitivity = attr_get_service_in.motion_detection_sensitivity, 
            )

        if not self.settings_sound_detection_callback == None:
            self.settings_sound_detection_callback(
                feature_enabled = attr_get_service_in.enable_sound_detection,
                enable = attr_get_service_in.sound_detection_switch,
                aging_type = attr_get_service_in.sound_detection_aging_type,
                sensitivity = attr_get_service_in.sound_detection_sensitivity, 
            )

        if not self.settings_cloud_video_recording_callback == None:
            self.settings_cloud_video_recording_callback(
                enable = attr_get_service_in.cloud_video_record_switch
            )

        if not self.settings_sound_callback == None:
            self.settings_sound_callback(
                feature_enabled = attr_get_service_in.enable_sound,
                enable = attr_get_service_in.sound_switch,
                aging_type = attr_get_service_in.sound_aging_type,
                volume = attr_get_service_in.volume,
            )

        if not self.settings_button_lights_callback == None:
            self.settings_button_lights_callback(
                feature_enabled = attr_get_service_in.enable_light,
                enable = attr_get_service_in.light_switch,
                aging_type = attr_get_service_in.light_aging_type,
            )

        if not self.state_power_callback == None:
            self.state_power_callback(
                battery_level = attr_get_service_in.electric_quantity,
                mode = attr_get_service_in.power_mode,
                type_ = attr_get_service_in.power_type,
            )

        if not self.state_food_callback == None:
            self.state_food_callback(
                motor_state = attr_get_service_in.motor_state,
                outlet_blocked = not attr_get_service_in.grain_outlet_state,
                low_fill_level = not attr_get_service_in.surplus_grain,
            )

        if not self.device_wifi_info_callback == None:
            self.device_wifi_info_callback(ssid = attr_get_service_in.wifi_ssid)

        if not self.device_sd_card_info_callback == None:
            self.device_sd_card_info_callback(
                state = attr_get_service_in.sd_card_state,
                file_system = attr_get_service_in.sd_card_file_system,
                total_capacity_mb = attr_get_service_in.sd_card_total_capacity,
                used_capacity_mb = attr_get_service_in.sd_card_used_capacity,
            )

        if not self.settings_feeding_video_callback == None:
            self.settings_feeding_video_callback(
                enable = attr_get_service_in.feeding_video_switch,
                video_on_start_feeding_plan = attr_get_service_in.enable_video_start_feeding_plan,
                video_after_manual_feeding = attr_get_service_in.enable_video_after_manual_feeding,
                recording_length_before_feeding_plan_time = attr_get_service_in.before_feeding_plan_time,
                recording_length_after_manual_feeding_time = attr_get_service_in.after_manual_feeding_time,
                video_watermark = attr_get_service_in.video_watermark_switch,
                automatic_recording = attr_get_service_in.automatic_recording,
            )

        if not self.settings_buttons_auto_lock_callback == None:
            self.settings_buttons_auto_lock_callback(
                enable = attr_get_service_in.auto_change_mode,
                threshold = attr_get_service_in.auto_threshold,
            )

        self._device_timestamp_sync_drift_check_and_adjust(attr_get_service_in.timestamp)

    def _attr_push_event_cb(self, attr_push_event_in: AttrPushEventIn):
        if not self.settings_audio_callback == None:
            self.settings_audio_callback(
                enable = attr_push_event_in.enable_audio,
                url = attr_push_event_in.audio_url,
            )

        # Update cached values
        if not attr_push_event_in.enable_audio == None:
            self.settings_audio_enabled = attr_push_event_in.enable_audio
        if not attr_push_event_in.audio_url == None:
            self.settings_audio_file_url = attr_push_event_in.audio_url

        if not self.settings_camera_callback == None:
            self.settings_camera_callback(
                feature_enabled = attr_push_event_in.enable_camera,
                enable = attr_push_event_in.camera_switch,
                aging_type = attr_push_event_in.camera_aging_type,
                night_vision = attr_push_event_in.night_vision,
                resolution = attr_push_event_in.resolution,
            )

        if not self.settings_recording_callback == None:
            self.settings_recording_callback(
                feature_enabled = attr_push_event_in.enable_video_record,
                enable = attr_push_event_in.video_record_switch,
                aging_type = attr_push_event_in.video_record_aging_type,
                mode = attr_push_event_in.video_record_mode,
            )

        if not self.settings_motion_detection_callback == None:
            self.settings_motion_detection_callback(
                feature_enabled = attr_push_event_in.enable_motion_detection,
                enable = attr_push_event_in.motion_detection_switch,
                aging_type = attr_push_event_in.motion_detection_aging_type,
                range_ = attr_push_event_in.motion_detection_range,
                sensitivity = attr_push_event_in.motion_detection_sensitivity, 
            )

        if not self.settings_sound_detection_callback == None:
            self.settings_sound_detection_callback(
                feature_enabled = attr_push_event_in.enable_sound_detection,
                enable = attr_push_event_in.sound_detection_switch,
                aging_type = attr_push_event_in.sound_detection_aging_type,
                sensitivity = attr_push_event_in.sound_detection_sensitivity, 
            )

        if not self.settings_cloud_video_recording_callback == None:
            self.settings_cloud_video_recording_callback(
                enable = attr_push_event_in.cloud_video_record_switch,
            )

        if not self.settings_sound_callback == None:
            self.settings_sound_callback(
                feature_enabled = attr_push_event_in.enable_sound,
                enable = attr_push_event_in.sound_switch,
                aging_type = attr_push_event_in.sound_aging_type,
                volume = attr_push_event_in.volume,
            )

        if not self.settings_button_lights_callback == None:
            self.settings_button_lights_callback(
                enable = attr_push_event_in.light_switch,
                aging_type = attr_push_event_in.light_aging_type,
            )

        if not self.state_power_callback == None:
            self.state_power_callback(
                battery_level = attr_push_event_in.electric_quantity,
                mode = attr_push_event_in.power_mode,
                type_ = attr_push_event_in.power_type,
            )

        if not self.state_food_callback == None:
            self.state_food_callback(
                motor_state = attr_push_event_in.motor_state,
                outlet_blocked = not attr_push_event_in.grain_outlet_state,
                low_fill_level = not attr_push_event_in.surplus_grain,
            )

        if not self.device_sd_card_info_callback == None:
            self.device_sd_card_info_callback(
                state = attr_push_event_in.sd_card_state,
                file_system = attr_push_event_in.sd_card_file_system,
                total_capacity_mb = attr_push_event_in.sd_card_total_capacity,
                used_capacity_mb = attr_push_event_in.sd_card_used_capacity,
            )

        if not self.settings_feeding_video_callback == None:
            self.settings_feeding_video_callback(
                enable = attr_push_event_in.feeding_video_switch,
                video_on_start_feeding_plan = attr_push_event_in.enable_video_start_feeding_plan,
                video_after_manual_feeding = attr_push_event_in.enable_video_after_manual_feeding,
                recording_length_before_feeding_plan_time = attr_push_event_in.before_feeding_plan_time,
                recording_length_after_manual_feeding_time = attr_push_event_in.after_manual_feeding_time,
                video_watermark = attr_push_event_in.video_watermark_switch,
                automatic_recording = attr_push_event_in.automatic_recording,
            )

        if not self.settings_buttons_auto_lock_callback == None:
            self.settings_buttons_auto_lock_callback(
                enable = attr_push_event_in.auto_change_mode,
                threshold = attr_push_event_in.auto_threshold,
            )

        attr_push_event_out = AttrPushEventOut.create(
            message_id = attr_push_event_in.message_id,
            code = Code.OK)
        self.client.attr_push_event_send(attr_push_event_out)

        self._device_timestamp_sync_drift_check_and_adjust(attr_push_event_in.timestamp)

    def _attr_set_service_cb(self, attr_set_service_in: AttrSetServiceIn):
        if attr_set_service_in.code != Code.OK:
            _error_report("Updating device attribute(s) failed")
            return

        self._device_timestamp_sync_drift_check_and_adjust(attr_set_service_in.timestamp)

    def _get_config_cb(self, get_config_in: GetConfigIn):
        if not self.device_info_callback == None:
            self.device_info_callback(
                product_id = get_config_in.product_id,
                hardware_version = get_config_in.hardware_version,
                software_version = get_config_in.software_version)

        if not self.device_wifi_info_callback == None:
            self.device_wifi_info_callback(mac_address = get_config_in.mac_address)

        self._device_timestamp_sync_drift_check_and_adjust(get_config_in.timestamp)

    def _get_feeding_plan_event_cb(self, get_feeding_plan_event_in: GetFeedingPlanEventIn):
        food_plans = self.food_plans

        get_feeding_plans_out: [GetFeedingPlanOut] = []

        timestamp_now = Timestamp.now()

        for food_plan in food_plans.plans:
            get_feeding_plan_out = GetFeedingPlanOut(
                plan_id = food_plan.id_,
                execution_time = food_plan.execution_time,
                repeat_day = food_plan.scheduled_days,
                enable_audio = food_plan.enable_audio,
                audio_times = food_plan.play_audio_times,
                grain_num = food_plan.grain_num,
                sync_time = timestamp_now,
            )

            get_feeding_plans_out.append(get_feeding_plan_out)

        get_feeding_plan_event_out = GetFeedingPlanEventOut(
            message_id = get_feeding_plan_event_in.message_id,
            timestamp = timestamp_now,
            code = Code.OK,
            plans = get_feeding_plans_out,
        )
        self.client.get_feeding_plan_event_send(get_feeding_plan_event_out)

        self._device_timestamp_sync_drift_check_and_adjust(get_feeding_plan_event_in.timestamp)

    def _manual_feeding_service_cb(self, manual_feeding_service_in: ManualFeedingServiceIn):
        if manual_feeding_service_in.code != Code.OK:
            _error_report("Manual feeding failed")
            return

        self._device_timestamp_sync_drift_check_and_adjust(manual_feeding_service_in.timestamp)

    def _feeding_plan_service_cb(self, feeding_plan_service_in: FeedingPlanServiceIn):
        if feeding_plan_service_in.code != Code.OK:
            _error_report("Configuring feeding plan failed")
            return

        # TODO verify further that feeding plans were actually correctly sync'd?

        self._device_timestamp_sync_drift_check_and_adjust(feeding_plan_service_in.timestamp)

    def _grain_output_event_cb(self, grain_output_event_in: GrainOutputEventIn):
        if grain_output_event_in.exec_step == ExecStep.GRAIN_START:
            if not self.food_output_log_start_callback == None:
                self.food_output_log_start_callback(grain_output_event_in.type_, grain_output_event_in.expected_grain_num)
            
            if not self.food_output_progress_callback == None:
                self.food_output_progress_callback(FoodOutputProgress.RUNNING)
        elif grain_output_event_in.exec_step == ExecStep.GRAIN_BLOCKING:
            if not self.food_output_progress_callback == None:
                self.food_output_progress_callback(FoodOutputProgress.BLOCKED)
        elif grain_output_event_in.exec_step == ExecStep.GRAIN_END:
            if not self.food_output_log_end_callback == None:
                self.food_output_log_end_callback(grain_output_event_in.type_, grain_output_event_in.actual_grain_num)
            
            if grain_output_event_in.expected_grain_num != grain_output_event_in.actual_grain_num:
                self._error_report("Food output actual != expected: {} != {}".format(grain_output_event_in.actual_grain_num, grain_output_event_in.expected_grain_num))

            if not self.food_output_progress_callback == None:
                self.food_output_progress_callback(FoodOutputProgress.IDLE)
        else:
            self.ad.error("Unhandled grain_output_event.exec_step: {}".format(grain_output_event_in.exec_step))

        grain_output_event_out = GrainOutputEventOut.create(
            message_id = grain_output_event_in.message_id,
            code = Code.OK,
            exec_step = grain_output_event_in.exec_step)
        self.client.grain_output_event_send(grain_output_event_out)

        self._device_timestamp_sync_drift_check_and_adjust(grain_output_event_in.timestamp)

    ##########################################

    def _heartbeat_watchdog_trigger(self):
        self.is_online = False

        if not self.went_offline_callback == None:
            self.went_offline_callback()

    def _device_timestamp_sync_drift_check_and_adjust(self, timestamp_device: Timestamp):
        timestamp_now = Timestamp.now()

        if not self._device_timestamp_sync_drift_check(timestamp_now, timestamp_device):
            self.ad.log("Device time drift detected, forcing NTP sync on device")

            ntp_sync_out = NtpSyncOut.create()
            self.client.ntp_sync_send(ntp_sync_out)

    def _device_timestamp_sync_drift_check(self, timestamp_backend: Timestamp, timestamp_device: Timestamp) -> bool:
        delta = timestamp_backend.abs_delta(timestamp_device)

        return delta < datetime.timedelta(seconds = self.NTP_SYNC_TIME_DIFF_THRESHOLD_SEC)

    def _device_food_plans_sync(self, food_plans: FoodPlans):
        feeding_plans_out: [FeedingPlanOut] = []

        sync_time_now = Timestamp.now()
        
        for food_plan in food_plans.plans:            
            feeding_plan_out = FeedingPlanOut(
                plan_id = food_plan.id_,
                execution_time = food_plan.execution_time,
                repeat_day = food_plan.scheduled_days,
                enable_audio = food_plan.enable_audio,
                audio_times = food_plan.play_audio_times,
                grain_num = food_plan.grain_num,
                sync_time = sync_time_now,
            )

            feeding_plans_out.append(feeding_plan_out)

        feeding_plan_service_out = FeedingPlanServiceOut.create(feeding_plans_out)
        self.client.feeding_plan_service_send(feeding_plan_service_out)

    def _error_report(self, message: str):
        if not self.error_callback == None:
            self.error_callback(message)

########################################################################################################################

class HomeAssistantDiscoveryMqtt:
    def __init__(self, mqtt: mqttapi.Mqtt, serial_number: str):
        self.mqtt: mqttapi.Mqtt = mqtt
        self.serial_number: str = serial_number

    def discovery_issue(self):
        self._ha_switch_config_publish('Feeding audio enable', 'mdi:account-voice', 'audio', 'enable', 'config')
        self._ha_text_config_publish('Feeding audio file url', 'mdi:account-voice', 'audio', 'file_url', 'config')

        self._ha_switch_config_publish('Camera enable', 'mdi:cctv', 'camera', 'enable', 'config')
        self._ha_binary_sensor_config_publish('Camera feature enabled', 'mdi:cctv', 'camera', 'enable', 'diagnostic')
        self._ha_select_config_publish('Camera aging type', 'mdi:cctv', 'camera', 'aging_type', [ AgingType.NON_SCHEDULED_ENABLED.name , AgingType.SCHEDULED_ENABLED.name ], 'config')
        self._ha_select_config_publish('Camera night vision', 'mdi:cctv', 'camera', 'night_vision', [ NightVision.AUTOMATIC.name, NightVision.OPEN.name, NightVision.CLOSE.name ], 'config')
        self._ha_select_config_publish('Camera resolution', 'mdi:cctv', 'camera', 'resolution', [ Resolution.P720.name, Resolution.P1080.name ], 'config')
        # TODO support aging type 2 items

        self._ha_switch_config_publish('Recording enable', 'mdi:camera', 'recording', 'enable', 'config')
        self._ha_binary_sensor_config_publish('Recording feature enabled', 'mdi:camera', 'recording', 'feature_enabled', 'diagnostic')
        self._ha_select_config_publish('Recording aging type', 'mdi:camera', 'recording', 'aging_type', [ AgingType.NON_SCHEDULED_ENABLED.name , AgingType.SCHEDULED_ENABLED.name ], 'config')
        self._ha_select_config_publish('Recording mode', 'mdi:camera', 'recording', 'mode', [ VideoRecordMode.CONTINUOUS.name, VideoRecordMode.MOTION_DETECTION.name ], 'config')
        # TODO support aging type 2 items

        self._ha_sensor_config_publish('SD card state', 'mdi:micro-sd', 'sd_card', 'state')
        self._ha_sensor_config_publish('SD card file system', 'mdi:micro-sd', 'sd_card', 'file_system')
        self._ha_sensor_config_publish('SD card total capacity', 'mdi:micro-sd', 'sd_card', 'total_capacity', unit_of_measurement = "MB")
        self._ha_sensor_config_publish('SD card used capacity', 'mdi:micro-sd', 'sd_card', 'used_capacity', unit_of_measurement = "MB")

        self._ha_switch_config_publish('Feeding video enable', 'mdi:movie', 'feeding_video', 'enable', 'config')
        self._ha_switch_config_publish('Feeding video on feeding plan trigger enabled', 'mdi:movie', 'feeding_video', 'on_feeding_plan_trigger_enable', 'config')
        self._ha_switch_config_publish('Feeding video on manual feeding trigger enabled', 'mdi:movie', 'feeding_video', 'on_manual_feeding_trigger_enable', 'config')
        self._ha_number_box_config_publish('Feeding video time before feeding plan trigger', 'mdi:movie', 'feeding_video', 'time_before_feeding_plan_trigger', 0, 60, 'config')
        self._ha_number_box_config_publish('Feeding video time after manual feeding trigger', 'mdi:movie', 'feeding_video', 'time_after_manual_feeding_trigger', 0, 60, 'config')
        self._ha_number_box_config_publish('Feeding video time automatic recording', 'mdi:movie', 'feeding_video', 'time_automatic_recording', 0, 60, 'config')
        self._ha_switch_config_publish('Feeding video watermark', 'mdi:movie', 'feeding_video', 'watermark', 'config')

        self._ha_switch_config_publish('Cloud video recording enable', 'mdi:cloud', 'cloud_video_recording', 'enable', 'config')

        self._ha_switch_config_publish('Motion detection enable', 'mdi:motion-sensor', 'motion_detection', 'enable', 'config')
        self._ha_binary_sensor_config_publish('Motion detection feature enabled', 'mdi:motion-sensor', 'motion_detection', 'feature_enabled', 'diagnostic')
        self._ha_select_config_publish('Motion detection aging type', 'mdi:motion-sensor', 'motion_detection', 'aging_type', [ AgingType.NON_SCHEDULED_ENABLED.name , AgingType.SCHEDULED_ENABLED.name ], 'config')
        self._ha_select_config_publish('Motion detection range', 'mdi:motion-sensor', 'motion_detection', 'range', [ MotionDetectionRange.SMALL.name, MotionDetectionRange.MEDIUM.name, MotionDetectionRange.LARGE.name ], 'config')
        self._ha_select_config_publish('Motion detection sensitivity', 'mdi:motion-sensor', 'motion_detection', 'sensitivity', [ MotionDetectionSensitivity.LOW.name, MotionDetectionSensitivity.MEDIUM.name, MotionDetectionSensitivity.HIGH.name ], 'config')
        # TODO support aging type 2 items

        self._ha_switch_config_publish('Sound detection on/off', 'mdi:bullhorn', 'sound_detection', 'enable', 'config')
        self._ha_binary_sensor_config_publish('Sound detection feature enabled', 'mdi:bullhorn', 'sound_detection', 'feature_enabled', 'diagnostic')
        self._ha_select_config_publish('Sound detection aging type', 'mdi:bullhorn', 'sound_detection', 'aging_type', [ AgingType.NON_SCHEDULED_ENABLED.name , AgingType.SCHEDULED_ENABLED.name ], 'config')
        self._ha_select_config_publish('Sound detection sensitivity', 'mdi:bullhorn', 'sound_detection', 'sensitivity', [ SoundDetectionSensitivity.LOW.name, SoundDetectionSensitivity.MEDIUM.name, SoundDetectionSensitivity.HIGH.name ], 'config')
        # TODO support aging type 2 items

        self._ha_switch_config_publish('Sound enable', 'mdi:speaker', 'sound', 'enable', 'config')
        self._ha_binary_sensor_config_publish('Sound feature enabled', 'mdi:speaker', 'sound', 'feature_enabled', 'diagnostic')
        self._ha_select_config_publish('Sound aging type', 'mdi:speaker', 'sound', 'aging_type', [ AgingType.NON_SCHEDULED_ENABLED.name , AgingType.SCHEDULED_ENABLED.name ], 'config')
        self._ha_number_slider_config_publish('Sound volume', 'mdi:speaker', 'sound', 'volume', 0, 100, 'config')
        # TODO support aging type 2 items

        self._ha_switch_config_publish('Button lights enable', 'mdi:lightbulb', 'button_lights', 'enable', 'config')
        self._ha_number_box_config_publish('Button lights aging type', 'mdi:lightbulb', 'button_lights', 'aging_type', [ AgingType.NON_SCHEDULED_ENABLED.name , AgingType.SCHEDULED_ENABLED.name ], 'config')

        self._ha_switch_config_publish('Buttons auto lock enable', 'mdi:lock', 'buttons_auto_lock', 'enable', 'config')
        self._ha_number_slider_config_publish('Buttons auto lock threshold', 'mdi:lock', 'buttons_auto_lock', 'threshold', 0, 100, 'config')

        self._ha_sensor_config_publish('Power battery level', 'mdi:lightning-bolt', 'power', 'battery_level')
        self._ha_sensor_config_publish('Power mode', 'mdi:lightning-bolt', 'power', 'mode')
        self._ha_sensor_config_publish('Power type', 'mdi:lightning-bolt', 'power', 'type')

        self._ha_connection_sensor_config_publish('Connection', 'device', 'online')
        self._ha_binary_sensor_config_publish('Error state', 'mdi:alert', 'device', 'error_state')
        self._ha_sensor_config_publish('Error message', 'mdi:alert', 'device', 'error_message', entity_category = 'diagnostic')
        self._ha_sensor_config_publish('Serial number', 'mdi:identifier', 'device', 'serial_number', entity_category = 'diagnostic')
        self._ha_sensor_config_publish('Software version', 'mdi:identifier', 'device', 'software_version', entity_category = 'diagnostic')
        self._ha_sensor_config_publish('Hardware version', 'mdi:identifier', 'device', 'hardware_version', entity_category = 'diagnostic')
        self._ha_sensor_config_publish('Product ID', 'mdi:identifier', 'device', 'product_id', entity_category = 'diagnostic')
        self._ha_sensor_config_publish('UUID', 'mdi:identifier', 'device', 'uuid', entity_category = 'diagnostic')
        self._ha_sensor_timestamp_config_publish('NTP last correct', 'mdi:clock', 'device', 'ntp_last_correct', entity_category = 'diagnostic')

        self._ha_sensor_config_publish('Wifi SSID', 'mdi:wifi', 'wifi', 'ssid', entity_category = 'diagnostic')
        self._ha_sensor_config_publish('Wifi rssi', 'mdi:wifi', 'wifi', 'rssi', unit_of_measurement = 'dBm', entity_category = 'diagnostic')
        self._ha_sensor_config_publish('Wifi type', 'mdi:wifi-cog', 'wifi', 'type', entity_category = 'diagnostic')
        self._ha_sensor_config_publish('Wifi mac address', 'mdi:wifi', 'wifi', 'mac_address', entity_category = 'diagnostic')

        self._ha_button_config_publish('Reboot', 'mdi:power', 'device', 'reboot', 'diagnostic')
        self._ha_button_config_publish('Factory reset', 'mdi:factory', 'device', 'factory_reset', 'diagnostic')
        self._ha_button_config_publish('Wifi force reconnect', 'mdi:wifi-cancel', 'device', 'wifi_reconnect', 'diagnostic')
        self._ha_button_config_publish('SD card format', 'mdi:delete', 'device', 'sd_card_format', 'diagnostic')

        self._ha_sensor_config_publish('Food motor state', 'mdi:food', 'food', 'motor_state', entity_category = 'diagnostic')
        self._ha_binary_sensor_config_publish('Food outlet blocked', 'mdi:food', 'food', 'outlet_blocked')
        self._ha_binary_sensor_config_publish('Food low fill level', 'mdi:food', 'food', 'low_fill_level')

        self._ha_text_config_publish('Food plan 1', 'mdi:food', 'food', 'plan_1', 'config')
        self._ha_text_config_publish('Food plan 2', 'mdi:food', 'food', 'plan_2', 'config')
        self._ha_text_config_publish('Food plan 3', 'mdi:food', 'food', 'plan_3', 'config')
        self._ha_text_config_publish('Food plan 4', 'mdi:food', 'food', 'plan_4', 'config')
        self._ha_text_config_publish('Food plan 5', 'mdi:food', 'food', 'plan_5', 'config')
        self._ha_text_config_publish('Food plan 6', 'mdi:food', 'food', 'plan_6', 'config')
        self._ha_text_config_publish('Food plan 7', 'mdi:food', 'food', 'plan_7', 'config')
        self._ha_text_config_publish('Food plan 8', 'mdi:food', 'food', 'plan_8', 'config')
        self._ha_text_config_publish('Food plan 9', 'mdi:food', 'food', 'plan_9', 'config')
        self._ha_button_config_publish('Manual feed', 'mdi:food', 'food', 'manual_feed')
        self._ha_number_slider_config_publish('Manual feed grain num', 'mdi:hamburger-plus', 'food', 'manual_feed_grain_num', 1, 24)
        
        self._ha_sensor_config_publish('Food output progress', 'mdi:food', 'food_output', 'progress')
        self._ha_sensor_timestamp_config_publish('Food output last start', 'mdi:food', 'food_output', 'last_start')
        self._ha_sensor_timestamp_config_publish('Food output last end', 'mdi:food', 'food_output', 'last_end')
        self._ha_sensor_config_publish('Food output last grain count', 'mdi:food', 'food_output', 'last_grain_count')
        self._ha_sensor_config_publish('Food output last trigger', 'mdi:food', 'food_output', 'last_trigger')

    ############################################################################

    def _ha_connection_sensor_config_publish(self, user_friendly_name: str, group: str, name: str):
        payload = {
            'name': user_friendly_name,
            'unique_id': self._config_unique_id_get(group, name),
            'device_class': 'connectivity',
            'state_topic': self._device_base_path_get('{}/{}'.format(group, name)),
            'payload_on': 'true',
            'payload_off': 'false',
        }

        merged_payload = payload | self._device_flags_get()

        self._mqtt_publish(self._ha_config_topic_base_path_get('binary_sensor', '{}_{}'.format(group, name)), merged_payload)

    def _ha_binary_sensor_config_publish(self, user_friendly_name: str, icon: str, group: str, name: str, entity_category: str = None):
        payload = {
            'name': user_friendly_name,
            'unique_id': self._config_unique_id_get(group, name),
            'state_topic': self._device_base_path_get('{}/{}'.format(group, name)),
            'icon': icon,
            'payload_on': 'true',
            'payload_off': 'false',
        }

        if not entity_category == None:
            payload = payload | { 'entity_category' : entity_category }

        merged_payload = payload | self._device_flags_get() | self._availability_flags_get()

        self._mqtt_publish(self._ha_config_topic_base_path_get('binary_sensor', '{}_{}'.format(group, name)), merged_payload)

    def _ha_button_config_publish(self, user_friendly_name: str, icon: str, group: str, name: str, entity_category: str = None):
        payload = {
            'name': user_friendly_name,
            'unique_id': self._config_unique_id_get(group, name),
            'command_topic': self._device_base_path_get('{}/cmd/{}'.format(group, name)),
            'payload_press': 'press',
            'icon': icon,
        }

        if not entity_category == None:
            payload = payload | { 'entity_category' : entity_category }

        merged_payload = payload | self._device_flags_get() | self._availability_flags_get()

        self._mqtt_publish(self._ha_config_topic_base_path_get('button', '{}_{}'.format(group, name)), merged_payload)

    def _ha_number_slider_config_publish(self, user_friendly_name: str, icon: str, group: str, name: str, min: int, max: int, entity_category: str = None):
        payload = {
            'name': user_friendly_name,
            'unique_id': self._config_unique_id_get(group, name),
            'command_topic': self._device_base_path_get('{}/cmd/{}'.format(group, name)),
            'state_topic': self._device_base_path_get('{}/{}'.format(group, name)),
            'min': min,
            'max': max,
            'mode': 'slider',
            'icon': icon,
        }

        if not entity_category == None:
            payload = payload | { 'entity_category' : entity_category }

        merged_payload = payload | self._device_flags_get() | self._availability_flags_get()

        self._mqtt_publish(self._ha_config_topic_base_path_get('number', '{}_{}'.format(group, name)), merged_payload)

    def _ha_number_box_config_publish(self, user_friendly_name: str, icon: str, group: str, name: str, min: int, max: int, entity_category: str = None):
        payload = {
            'name': user_friendly_name,
            'unique_id': self._config_unique_id_get(group, name),
            'command_topic': self._device_base_path_get('{}/cmd/{}'.format(group, name)),
            'state_topic': self._device_base_path_get('{}/{}'.format(group, name)),
            'min': min,
            'max': max,
            'mode': 'box',
            'icon': icon,
        }

        if not entity_category == None:
            payload = payload | { 'entity_category' : entity_category }

        merged_payload = payload | self._device_flags_get() | self._availability_flags_get()

        self._mqtt_publish(self._ha_config_topic_base_path_get('number', '{}_{}'.format(group, name)), merged_payload)

    def _ha_select_config_publish(self, user_friendly_name: str, icon: str, group: str, name: str, options: [str], entity_category: str = None):
        payload = {
            'name': user_friendly_name,
            'unique_id': self._config_unique_id_get(group, name),
            'icon': icon,
            'command_topic': self._device_base_path_get('{}/cmd/{}'.format(group, name)),
            'state_topic': self._device_base_path_get('{}/{}'.format(group, name)),
            'optimistic': 'false',
            'options': options,
        }

        if not entity_category == None:
            payload = payload | { 'entity_category' : entity_category }

        merged_payload = payload | self._device_flags_get() | self._availability_flags_get()

        self._mqtt_publish(self._ha_config_topic_base_path_get('select', '{}_{}'.format(group, name)), merged_payload)

    def _ha_sensor_config_publish(self, user_friendly_name: str, icon: str, group: str, name: str, unit_of_measurement: str = None, entity_category: str = None):
        payload = {
            'name': user_friendly_name,
            'unique_id': self._config_unique_id_get(group, name),
            'icon': icon,
            'state_topic': self._device_base_path_get('{}/{}'.format(group, name)),
        }

        if not unit_of_measurement == None:
            payload = payload | { 'unit_of_measurement': unit_of_measurement }

        if not entity_category == None:
            payload = payload | { 'entity_category' : entity_category }

        merged_payload = payload | self._device_flags_get() | self._availability_flags_get()

        self._mqtt_publish(self._ha_config_topic_base_path_get('sensor', '{}_{}'.format(group, name)), merged_payload)

    def _ha_sensor_timestamp_config_publish(self, user_friendly_name: str, icon: str, group: str, name: str, entity_category: str = None):
        payload = {
            'name': user_friendly_name,
            'unique_id': self._config_unique_id_get(group, name),
            'icon': icon,
            'state_topic': self._device_base_path_get('{}/{}'.format(group, name)),
            'device_class': 'timestamp',
            'value_template': '{{ as_datetime(value) }}'
        }

        if not entity_category == None:
            payload = payload | { 'entity_category' : entity_category }

        merged_payload = payload | self._device_flags_get() | self._availability_flags_get()

        self._mqtt_publish(self._ha_config_topic_base_path_get('sensor', '{}_{}'.format(group, name)), merged_payload)

    def _ha_switch_config_publish(self, user_friendly_name: str, icon: str, group: str, name: str, entity_category: str = None):
        payload = {
            'name': user_friendly_name,
            'unique_id': self._config_unique_id_get(group, name),
            'device_class': 'switch',
            'icon': icon,
            'command_topic': self._device_base_path_get('{}/cmd/{}'.format(group, name)),
            'state_topic': self._device_base_path_get('{}/{}'.format(group, name)),
            'optimistic': 'false',
            'payload_on': 'true',
            'payload_off': 'false',
            'state_on': 'true',
            'state_off': 'false',
        }

        if not entity_category == None:
            payload = payload | { 'entity_category' : entity_category }

        merged_payload = payload | self._device_flags_get() | self._availability_flags_get()

        self._mqtt_publish(self._ha_config_topic_base_path_get('switch', '{}_{}'.format(group, name)), merged_payload)

    def _ha_text_config_publish(self, user_friendly_name: str, icon: str, group: str, name: str, entity_category: str = None):
        payload = {
            'name': user_friendly_name,
            'unique_id': self._config_unique_id_get(group, name),
            'icon': icon,
            'mode': 'text',
            'command_topic': self._device_base_path_get('{}/cmd/{}'.format(group, name)),
            'state_topic': self._device_base_path_get('{}/{}'.format(group, name)),
        }

        if not entity_category == None:
            payload = payload | { 'entity_category' : entity_category }

        merged_payload = payload | self._device_flags_get() | self._availability_flags_get()

        self._mqtt_publish(self._ha_config_topic_base_path_get('text', '{}_{}'.format(group, name)), merged_payload)

    def _device_flags_get(self):
        return {
            'device': self._device_info_get(),
        }

    def _availability_flags_get(self):
        return {
            'availability_topic': self._device_base_path_get('device/online'),
            'payload_available': 'true',
            'payload_not_available': 'false',
        }

    def _device_info_get(self):
        return {
            'identifiers': 'plaf203',
            'name': 'Pet libro cat feeder',
            'model': 'PLAF203',
            'manufacturer': 'Pet libro',
            'sw_version': 'unknown',
            'serial_number': self.serial_number,
        }

    def _mqtt_publish(self, topic: str, payload: dict):
        payload_json = json.dumps(payload)
        self.mqtt.mqtt_publish(topic, payload_json, namespace = "mqtt", retain = True)

    def _config_unique_id_get(self, type_: str, name: str):
        return "plaf203_{}_{}_{}".format(self.serial_number, type_, name)

    def _device_base_path_get(self, topic: str):
        return "plaf203/{}/{}".format(self.serial_number, topic)

    def _ha_config_topic_base_path_get(self, component: str, name: str):
        return "homeassistant/{}/plaf203/{}/config".format(component, name)

########################################################################################################################

# Store in a dedicated namespace that is written to disk
# Make sure this is configured in your appdaemon.yaml under the appdaemon parent like follows:
#  namespaces:
#    plaf203:
#      writeback: safe

class Storage:
    def __init__(self, ad: adapi.ADAPI, namespace: str, serial_number: str):
        self.ad: adapi.ADAPI = ad
        self.namespace: str = namespace
        self.food_manual_feed_grain_num_entity_id: str = 'sensor.plaf203_{}_food_manual_feed_grain_num'.format(serial_number)
        self.food_plans_entity_id: str = 'text.plaf203_{}_food_plans'.format(serial_number)

    def initialize(self):
        self.ad.set_namespace('plaf203')

        if not self._entity_state_exists(self.food_manual_feed_grain_num_entity_id):
            self._entity_state_int_set(self.food_manual_feed_grain_num_entity_id, 1)

        if not self._entity_state_exists(self.food_plans_entity_id):
            self._entity_state_dict_set(self.food_plans_entity_id, FoodPlans.create_empty().to_dict())

    def terminate(self):
        self.ad.save_namespace()

    def food_manual_feed_grain_num_get(self) -> int:
        return self._entity_state_int_get(self.food_manual_feed_grain_num_entity_id)

    def food_plans_get(self) -> FoodPlans:
        data = self._entity_state_dict_get(self.food_plans_entity_id)
        return FoodPlans.from_dict(data)

    def food_manual_feed_grain_num_set(self, grain_num: int):
        self._entity_state_int_set(self.food_manual_feed_grain_num_entity_id, grain_num)

    def food_plans_set(self, food_plans: FoodPlans):
        self._entity_state_dict_set(self.food_plans_entity_id, food_plans.to_dict())

    def _entity_state_exists(self, name: str) -> bool:
        return not self.ad.get_state(name, namespace = 'plaf203') == None

    def _entity_state_dict_get(self, name: str) -> dict:
        json_str = self.ad.get_state(name, namespace = 'plaf203')
        return json.loads(json_str)

    def _entity_state_int_get(self, name: str) -> str:
        return self.ad.get_state(name, namespace = 'plaf203')

    def _entity_state_dict_set(self, name: str, state: dict):
        json_str = json.dumps(state)
        self.ad.set_state(
            name,
            state = json_str,
            namespace = 'plaf203')

    def _entity_state_int_set(self, name: str, state: int):
        self.ad.set_state(
            name,
            state = state,
            namespace = 'plaf203')

########################################################################################################################

class Plaf203(adbase.ADBase):
    def initialize(self):
        mqtt_host: str = self.args['mqtt_host']
        mqtt_port: int = self.args['mqtt_port']
        self.serial_number: str = self.args['serial_number']

        self.ad: adapi.ADAPI = self.get_ad_api()
        self.mqtt: mqttapi.Mqtt = self.get_plugin_api("MQTT")

        self.ad.log("Initializing plaf203, serial number {}".format(self.serial_number))

        self.storage = Storage(self.ad, 'plaf203', self.serial_number)
        self.storage.initialize()
        self._persistent_state_recover()

        self.backend = Backend()
        self.backend.initialize(self.ad, self.mqtt, self.serial_number, mqtt_host, mqtt_port, self.storage.food_plans_get())

        self._backend_listeners_register()

        self.hass_discovery = HomeAssistantDiscoveryMqtt(self.mqtt, self.serial_number)
        self.hass_discovery.discovery_issue()

        # When restarting, always flag the device offline initially
        # This ensures the integration waits for an actual life signal by the device
        # and re-initializes all state based on the device's current state
        
        self._device_online_set(False)

        self._user_input_topics_subscribe()

    def terminate(self):
        self.storage.terminate()

        # Mark the device state as not connected to detect if something is wrong
        # when the app crashes because it also calls terminate still
        self._device_online_set(False)

    ############################################################################

    def _backend_listeners_register(self):
        self.backend.went_online_listen(self._went_online_cb)
        self.backend.went_offline_listen(self._went_offline_cb)
        self.backend.ntp_sync_status_listen(self._ntp_sync_status_cb)
        self.backend.error_listen(self._error_cb)

        self.backend.device_info_listen(self._device_info_cb)
        self.backend.device_wifi_info_listen(self._device_wifi_info_cb)
        self.backend.device_sd_card_info_listen(self._device_sd_card_info_cb)

        self.backend.settings_audio_listen(self._settings_audio_cb)
        self.backend.settings_camera_listen(self._settings_camera_cb)
        self.backend.settings_recording_listen(self._settings_recording_cb)
        self.backend.settings_motion_detection_listen(self._settings_motion_detection_cb)
        self.backend.settings_sound_detection_listen(self._settings_sound_detection_cb)
        self.backend.settings_cloud_video_recording_listen(self._settings_cloud_video_recording_cb)
        self.backend.settings_sound_listen(self._settings_sound_cb)
        self.backend.settings_button_lights_listen(self._settings_button_lights_cb)
        self.backend.settings_feeding_video_listen(self._settings_feeding_video_cb)
        self.backend.settings_buttons_auto_lock_listen(self._settings_buttons_auto_lock_cb)

        self.backend.state_power_listen(self._state_power_cb)
        self.backend.state_food_listen(self._state_food_cb)

        self.backend.food_output_log_start_listen(self._food_output_log_start_cb)
        self.backend.food_output_log_end_listen(self._food_output_log_end_cb)
        self.backend.food_output_progress_listen(self._food_output_progress_cb)

    def _persistent_state_recover(self):
        self.ad.log("Recovering persistant state")

        manual_feed_grain_num = self.storage.food_manual_feed_grain_num_get()
        self._food_manual_feed_grain_num_set(manual_feed_grain_num)

        food_plans: FoodPlans = self.storage.food_plans_get()

        self.ad.log("Stored food plans: {}".format(food_plans))

        self._food_plans_set(food_plans)

    def _user_input_topics_subscribe(self):
        self._mqtt_subscribe('audio/cmd/enable', self._mqtt_cmd_audio_enable_cb)
        self._mqtt_subscribe('audio/cmd/file_url', self._mqtt_cmd_audio_file_url_cb)

        self._mqtt_subscribe('camera/cmd/enable', self._mqtt_cmd_camera_enable_cb)
        self._mqtt_subscribe('camera/cmd/aging_type', self._mqtt_cmd_camera_aging_type_cb)
        self._mqtt_subscribe('camera/cmd/night_vision', self._mqtt_cmd_camera_night_vision_cb)
        self._mqtt_subscribe('camera/cmd/resolution', self._mqtt_cmd_camera_resolution_cb)

        self._mqtt_subscribe('recording/cmd/enable', self._mqtt_cmd_recording_enable_cb)
        self._mqtt_subscribe('recording/cmd/aging_type', self._mqtt_cmd_recording_aging_type_cb)
        self._mqtt_subscribe('recording/cmd/mode', self._mqtt_cmd_recording_mode_cb)

        self._mqtt_subscribe('sound/cmd/enable', self._mqtt_cmd_sound_enable_cb)
        self._mqtt_subscribe('sound/cmd/aging_type', self._mqtt_cmd_sound_aging_type_cb)
        self._mqtt_subscribe('sound/cmd/volume', self._mqtt_cmd_sound_volume_cb)

        self._mqtt_subscribe('feeding_video/cmd/enable', self._mqtt_cmd_feeding_video_enable_cb)
        self._mqtt_subscribe('feeding_video/cmd/on_feeding_plan_trigger_enable', self._mqtt_cmd_feeding_video_on_feeding_plan_trigger_enable_cb)
        self._mqtt_subscribe('feeding_video/cmd/on_manual_feeding_trigger_enable', self._mqtt_cmd_feeding_video_on_manual_feeding_trigger_enable_cb)
        self._mqtt_subscribe('feeding_video/cmd/time_before_feeding_plan_trigger', self._mqtt_cmd_feeding_video_time_before_feeding_plan_trigger_cb)
        self._mqtt_subscribe('feeding_video/cmd/time_after_manual_feeding_trigger', self._mqtt_cmd_feeding_video_time_after_manual_feeding_trigger_cb)
        self._mqtt_subscribe('feeding_video/cmd/time_automatic_recording', self._mqtt_cmd_feeding_video_time_automatic_recording_cb)
        self._mqtt_subscribe('feeding_video/cmd/watermark', self._mqtt_cmd_feeding_video_watermark_cb)

        self._mqtt_subscribe('motion_detection/cmd/enable', self._mqtt_cmd_motion_detection_enable_cb)
        self._mqtt_subscribe('motion_detection/cmd/aging_type', self._mqtt_cmd_motion_detection_aging_type_cb)
        self._mqtt_subscribe('motion_detection/cmd/range', self._mqtt_cmd_motion_detection_range_cb)
        self._mqtt_subscribe('motion_detection/cmd/sensitivity', self._mqtt_cmd_motion_detection_sensitivity_cb)

        self._mqtt_subscribe('sound_detection/cmd/enable', self._mqtt_cmd_sound_detection_enable_cb)
        self._mqtt_subscribe('sound_detection/cmd/aging_type', self._mqtt_cmd_sound_detection_aging_type_cb)
        self._mqtt_subscribe('sound_detection/cmd/sensitivity', self._mqtt_cmd_sound_detection_sensitivity_cb)

        self._mqtt_subscribe('cloud_video_recording/cmd/enable', self._mqtt_cmd_cloud_video_recording_enable_cb)

        self._mqtt_subscribe('buttons_auto_lock/cmd/enable', self._mqtt_cmd_buttons_auto_lock_enable_cb)
        self._mqtt_subscribe('buttons_auto_lock/cmd/thresold', self._mqtt_cmd_buttons_auto_lock_threshold_cb)

        self._mqtt_subscribe('button_lights/cmd/enable', self._mqtt_cmd_button_light_enable_cb)
        self._mqtt_subscribe('button_lights/cmd/aging_type', self._mqtt_cmd_button_light_aging_type_cb)

        self._mqtt_subscribe('food/cmd/plan_1', self._mqtt_cmd_food_plans)
        self._mqtt_subscribe('food/cmd/plan_2', self._mqtt_cmd_food_plans)
        self._mqtt_subscribe('food/cmd/plan_3', self._mqtt_cmd_food_plans)
        self._mqtt_subscribe('food/cmd/plan_4', self._mqtt_cmd_food_plans)
        self._mqtt_subscribe('food/cmd/plan_5', self._mqtt_cmd_food_plans)
        self._mqtt_subscribe('food/cmd/plan_6', self._mqtt_cmd_food_plans)
        self._mqtt_subscribe('food/cmd/plan_7', self._mqtt_cmd_food_plans)
        self._mqtt_subscribe('food/cmd/plan_8', self._mqtt_cmd_food_plans)
        self._mqtt_subscribe('food/cmd/plan_9', self._mqtt_cmd_food_plans)
        self._mqtt_subscribe('food/cmd/manual_feed', self._mqtt_cmd_manual_feed_cb)
        self._mqtt_subscribe('food/cmd/manual_feed_grain_num', self._mqtt_cmd_manual_feed_grain_num_cb)

        self._mqtt_subscribe('device/cmd/reboot', self._mqtt_cmd_device_reboot)
        self._mqtt_subscribe('device/cmd/factory_reset', self._mqtt_cmd_device_factory_reset)
        self._mqtt_subscribe('device/cmd/wifi_reconnect', self._mqtt_cmd_device_wifi_reconnect)
        self._mqtt_subscribe('device/cmd/sd_card_format', self._mqtt_cmd_device_sd_card_format)

    ############################################################################

    def _went_online_cb(self):
        self.ad.log("Went online")
        self._device_online_set(True)

        # Clear error state
        self._device_error_state_set(False)
        self._device_error_message_set("No error")

    def _went_offline_cb(self):
        self.ad.log("Went offline")
        self._device_online_set(False)

    def _ntp_sync_status_cb(self, successful_correction: bool):
        if successful_correction == True:
            self.ad.log("NTP sync on device corrected")
            now = datetime.datetime.now().astimezone()
            self._device_ntp_last_correct(now)
        else:
            self.ad.log("NTP sync correction on device failed")
            self._device_error_state_set(True)
            self._device_error_message_set("NTP sync with device failed")

    def _error_cb(self, message: str):
        self.ad.error("Error: {}".format(message))
        self._device_error_state_set(True)
        self._device_error_message_set(message)

    def _device_info_cb(self, device_serial: str = None, product_id: str = None, uuid: str = None, hardware_version: str = None, software_version: str = None):
        self.ad.log("Device info: {}, {}, {}, {}, {}".format(device_serial, product_id, uuid, hardware_version, software_version))
        
        if not device_serial == None:
            self._device_serial_number_set(device_serial)

        if not product_id == None:
            self._device_product_id_set(product_id)

        if not uuid == None:
            self._device_uuid_set(uuid)

        if not hardware_version == None:
            self._device_hardware_version_set(hardware_version)

        if not software_version == None:
            self._device_software_version_set(software_version)

    def _device_wifi_info_cb(self, mac_address: str = None, rssi: int = None, type_: WifiType = None, ssid: str = None):
        self.ad.log("Device wifi info: {}, {}, {}, {}".format(mac_address, rssi, type_, ssid))
        
        if not mac_address == None:
            self._wifi_mac_address_set(mac_address)

        if not rssi == None:
            self._wifi_rssi_set(rssi)

        if not type_ == None:
            self._wifi_type_set(type_)

        if not ssid == None:
            self._wifi_ssid_set(ssid)

    def _device_sd_card_info_cb(self, state: SdCardState = None, file_system: SdCardFileSystem = None, total_capacity_mb: int = None, used_capacity_mb: int = None):
        self.ad.log("Device SD card info: {}, {}, {}, {}".format(state, file_system, total_capacity_mb, used_capacity_mb))
        
        if not state == None:
            self._sd_card_state_set(state)

        if not file_system == None:
            self._sd_card_file_system_set(file_system)

        if not total_capacity_mb == None:
            self._sd_card_total_capacity_set(total_capacity_mb)

        if not used_capacity_mb == None:
            self._sd_card_used_capacity_set(used_capacity_mb)

    def _settings_audio_cb(self, enable: bool = None, url: str = None):
        self.ad.log("Settings audio: {}, {}".format(enable, url))

        if not enable == None:
            self._audio_enable_set(enable)

        if not url == None:
            self._audio_url_set(url)

    def _settings_camera_cb(self, feature_enabled: bool = None, enable: bool = None, aging_type: AgingType = None, night_vision: NightVision = None, resolution: Resolution = None):
        self.ad.log("Settings camera: {}, {}, {}, {}, {}".format(feature_enabled, enable, aging_type, night_vision, resolution))
        
        if not feature_enabled == None:
            self._camera_feature_enabled_set(feature_enabled)

        if not enable == None:
            self._camera_enable_set(enable)

        if not aging_type == None:
            self._camera_aging_type_set(aging_type)

        if not night_vision == None:
            self._camera_night_vision_set(night_vision)

        if not resolution == None:
            self._camera_resolution_set(resolution)

    def _settings_recording_cb(self, feature_enabled: bool = None, enable: bool = None, aging_type: AgingType = None, mode: VideoRecordMode = None):
        self.ad.log("Settings recording: {}, {}, {}, {}".format(feature_enabled, enable, aging_type, mode))
        
        if not feature_enabled == None:
            self._recording_feature_enabled_set(feature_enabled)

        if not enable == None:
            self._recording_enable_set(enable)

        if not aging_type == None:
            self._recording_aging_type_set(aging_type)

        if not mode == None:
            self._recording_mode_set(mode)

    def _settings_motion_detection_cb(self, feature_enabled: bool = None, enable: bool = None, aging_type: AgingType = None, range_: MotionDetectionRange = None, sensitivity: MotionDetectionSensitivity = None):
        self.ad.log("Settings motion detection: {}, {}, {}, {}, {}".format(feature_enabled, enable, aging_type, range_, sensitivity))
        
        if not feature_enabled == None:
            self._motion_detection_feature_enabled_set(feature_enabled)

        if not enable == None:
            self._motion_detection_enable_set(enable)

        if not aging_type == None:
            self._motion_detection_aging_type_set(aging_type)

        if not range_ == None:
            self._motion_detection_range_set(range_)

        if not sensitivity == None:
            self._motion_detection_sensitivity_set(sensitivity)

    def _settings_sound_detection_cb(self, feature_enabled: bool = None, enable: bool = None, aging_type: AgingType = None, sensitivity: SoundDetectionSensitivity = None):
        self.ad.log("Settings sound detection: {}, {}, {}, {}".format(feature_enabled, enable, aging_type, sensitivity))
        
        if not feature_enabled == None:
            self._sound_detection_feature_enabled_set(feature_enabled)

        if not enable == None:
            self._sound_detection_enable_set(enable)

        if not aging_type == None:
            self._sound_detection_aging_type_set(aging_type)

        if not sensitivity == None:
            self._sound_detection_sensitivity_set(sensitivity)

    def _settings_cloud_video_recording_cb(self, enable: bool):
        self.ad.log("Settings cloud video recording: {}".format(enable))
        
        if not enable == None:
            self._cloud_video_recording_enable_set(enable)

    def _settings_sound_cb(self, feature_enabled: bool = None, enable: bool = None, aging_type: AgingType = None, volume: PercentageInt = None):
        self.ad.log("Settings sound: {}, {}, {}, {}".format(feature_enabled, enable, aging_type, volume))
        
        if not feature_enabled == None:
            self._sound_feature_enabled_set(feature_enabled)

        if not enable == None:
            self._sound_enable_set(enable)

        if not aging_type == None:
            self._sound_aging_type_set(aging_type)

        if not volume == None:
            self._sound_volume_set(volume)

    def _settings_button_lights_cb(self, feature_enabled: bool = None, enable: bool = None, aging_type: AgingType = None):
        self.ad.log("Settings light: {}, {}, {}".format(feature_enabled, enable, aging_type))
        
        if not feature_enabled == None:
            self._button_lights_feature_enabled_set(feature_enabled)

        if not enable == None:
            self._button_lights_enable_set(enable)

        if not aging_type == None:
            self._button_lights_aging_type_set(aging_type)

    def _settings_feeding_video_cb(self, enable: bool = None, video_on_start_feeding_plan: bool = None, video_after_manual_feeding: bool = None, recording_length_before_feeding_plan_time: int = None, recording_length_after_manual_feeding_time: int = None, video_watermark: bool = None, automatic_recording: int = None):
        self.ad.log("Settings feeding video: {}, {}, {}, {}, {}, {}, {}".format(
            enable,
            video_on_start_feeding_plan,
            video_after_manual_feeding,
            recording_length_before_feeding_plan_time,
            recording_length_after_manual_feeding_time,
            video_watermark,
            automatic_recording
        ))
        
        if not enable == None:
            self._feeding_video_enable(enable)

        if not video_on_start_feeding_plan == None:
            self._feeding_video_on_feeding_plan_trigger_enable(video_on_start_feeding_plan)

        if not video_after_manual_feeding == None:
            self._feeding_video_on_manual_feeding_trigger_enable(video_after_manual_feeding)

        if not recording_length_before_feeding_plan_time == None:
            self._feeding_video_time_before_feeding_plan_trigger(recording_length_before_feeding_plan_time)

        if not recording_length_after_manual_feeding_time == None:
            self._feeding_video_time_after_manual_feeding_trigger(recording_length_after_manual_feeding_time)

        if not automatic_recording == None:
            self._feeding_video_time_automatic_recording(automatic_recording)

        if not video_watermark == None:
            self._feeding_video_watermark(video_watermark)

    def _settings_buttons_auto_lock_cb(self, enable: bool = None, threshold: int = None):
        self.ad.log("Settings buttons auto lock: {}, {}".format(enable, threshold))

        if not enable == None:
            self._buttons_auto_lock_enable_set(enable)

        if not threshold == None:
            self._buttons_auto_lock_threshold_set(threshold)

    def _state_power_cb(self, battery_level: PercentInt = None, mode: PowerMode = None, type_: PowerType = None):
        self.ad.log("State power: {}, {}, {}".format(battery_level, mode, type_))

        if not battery_level == None:
            self._power_battery_level_set(battery_level)

        if not mode == None:
            self._power_mode_set(mode)

        if not type_ == None:
            self._power_type_set(type_)

    def _state_food_cb(self, motor_state: int = None, outlet_blocked: bool = None, low_fill_level: bool = None):
        self.ad.log("State food: {}, {}, {}".format(motor_state, outlet_blocked, low_fill_level))

        if not motor_state == None:
            self._food_motor_state_set(motor_state)

        if not outlet_blocked == None:
            self._food_outlet_blocked_set(outlet_blocked)

        if not low_fill_level == None:
            self._food_low_fill_level_set(low_fill_level)

    def _food_output_log_start_cb(self, grain_output_type: GrainOutputType, grain_num: int):
        self.ad.log("Food output start: {}, {}".format(grain_output_type, grain_num))

        now = datetime.datetime.now().astimezone()
        self._food_output_last_start_set(now)
        self._food_output_last_grain_count_set(grain_num)
        self._food_output_last_trigger_set(grain_output_type)

    def _food_output_log_end_cb(self, grain_output_type: GrainOutputType, grain_num: int):
        self.ad.log("Food output end: {}, {}".format(grain_output_type, grain_num))

        now = datetime.datetime.now().astimezone()
        self._food_output_last_end_set(now)

    def _food_output_progress_cb(self, food_output_progress: FoodOutputProgress):
        self.ad.log("Food output progress: {}".format(food_output_progress))

        self._food_output_progress_set(food_output_progress)

    ############################################################################

    def _device_online_set(self, online: bool):
        self._mqtt_publish_bool('device/online', online)

    def _device_error_state_set(self, is_error: bool):
        self._mqtt_publish_bool('device/error_state', is_error)

    def _device_error_message_set(self, message: str):
        self._mqtt_publish_str('device/error_message', message)

    def _device_serial_number_set(self, serial_number: str):
        self._mqtt_publish_str('device/serial_number', serial_number) 

    def _device_hardware_version_set(self, hardware_version: str):
        self._mqtt_publish_str('device/hardware_version', hardware_version) 

    def _device_software_version_set(self, software_version: str):
        self._mqtt_publish_str('device/software_version', software_version)

    def _device_product_id_set(self, product_id: str):
        self._mqtt_publish_str('device/product_id', product_id)

    def _device_uuid_set(self, uuid: str):
        self._mqtt_publish_str('device/uuid', uuid)

    def _device_ntp_last_correct(self, datetime_: datetime.datetime):
        self._mqtt_publish_datetime('device/ntp_last_correct', datetime_)

    #########################

    def _wifi_ssid_set(self, ssid: str):
        self._mqtt_publish_str('wifi/ssid', ssid)

    def _wifi_rssi_set(self, rssi: int):
        self._mqtt_publish_int('wifi/rssi', rssi)

    def _wifi_type_set(self, type_: WifiType):
        self._mqtt_publish_str('wifi/type', type_.name)

    def _wifi_mac_address_set(self, mac_address: str):
        self._mqtt_publish_str('wifi/mac_address', mac_address)

    #########################

    def _sd_card_state_set(self, state: SdCardState):
        self._mqtt_publish_str('sd_card/state', state.name)

    def _sd_card_file_system_set(self, file_system: SdCardFileSystem):
        self._mqtt_publish_str('sd_card/file_system', file_system.name)

    def _sd_card_total_capacity_set(self, total_capacity: int):
        self._mqtt_publish_int('sd_card/total_capacity', total_capacity)

    def _sd_card_used_capacity_set(self, used_capacity: int):
        self._mqtt_publish_int('sd_card/used_capacity', used_capacity)

    #########################

    def _audio_enable_set(self, enable: bool):
        self._mqtt_publish_bool('audio/enable', enable)

    def _audio_url_set(self, url: str):
        self._mqtt_publish_str('audio/file_url', url)

    #########################

    def _food_output_progress_set(self, output_progress: str):
        self._mqtt_publish_str('food/output_progress', output_progress)

    #########################

    def _camera_feature_enabled_set(self, feature_enabled: bool):
        self._mqtt_publish_bool('camera/feature_enabled', feature_enabled)

    def _camera_enable_set(self, enable: bool):
        self._mqtt_publish_bool('camera/enable', enable)

    def _camera_aging_type_set(self, aging_type: AgingType):
        self._mqtt_publish_str('camera/aging_type', aging_type.name)

    def _camera_night_vision_set(self, night_vision: NightVision):
        self._mqtt_publish_str('camera/night_vision', night_vision.name)

    def _camera_resolution_set(self, resolution: Resolution):
        self._mqtt_publish_str('camera/resolution', resolution.name)

    #########################

    def _recording_feature_enabled_set(self, feature_enabled: bool):
        self._mqtt_publish_bool('recording/feature_enabled', feature_enabled)

    def _recording_enable_set(self, enable: bool):
        self._mqtt_publish_bool('recording/enable', enable)

    def _recording_aging_type_set(self, aging_type: AgingType):
        self._mqtt_publish_str('recording/aging_type', aging_type.name)

    def _recording_mode_set(self, mode: VideoRecordMode):
        self._mqtt_publish_str('recording/mode', mode.name)

    #########################

    def _motion_detection_feature_enabled_set(self, feature_enabled: bool):
        self._mqtt_publish_bool('motion_detection/feature_enabled', feature_enabled)

    def _motion_detection_enable_set(self, enable: bool):
        self._mqtt_publish_bool('motion_detection/enable', enable)

    def _motion_detection_aging_type_set(self, aging_type: AgingType):
        self._mqtt_publish_str('motion_detection/aging_type', aging_type.name)

    def _motion_detection_range_set(self, range: MotionDetectionRange):
        self._mqtt_publish_str('motion_detection/range', range.name)

    def _motion_detection_sensitivity_set(self, sensitivity: MotionDetectionSensitivity):
        self._mqtt_publish_str('motion_detection/sensitivity', sensitivity.name)

    #########################

    def _sound_detection_feature_enabled_set(self, feature_enabled: bool):
        self._mqtt_publish_bool('sound_detection/feature_enabled', feature_enabled)

    def _sound_detection_enable_set(self, enable: bool):
        self._mqtt_publish_bool('sound_detection/enable', enable)

    def _sound_detection_aging_type_set(self, aging_type: AgingType):
        self._mqtt_publish_str('sound_detection/aging_type', aging_type.name)

    def _sound_detection_sensitivity_set(self, sensitivity: SoundDetectionSensitivity):
        self._mqtt_publish_str('sound_detection/sensitivity', sensitivity.name)

    #########################

    def _cloud_video_recording_enable_set(self, enable: bool):
        self._mqtt_publish_bool('cloud_video_recording/enable', enable)

    #########################

    def _sound_feature_enabled_set(self, feature_enabled: bool):
        self._mqtt_publish_bool('sound/feature_enabled', feature_enabled)

    def _sound_enable_set(self, enable: bool):
        self._mqtt_publish_bool('sound/enable', enable)

    def _sound_aging_type_set(self, aging_type: AgingType):
        self._mqtt_publish_str('sound/aging_type', aging_type.name)

    def _sound_volume_set(self, volume: PercentInt):
        self._mqtt_publish_int('sound/volume', volume.value_get())

    #########################

    def _button_lights_feature_enabled_set(self, feature_enabled: bool):
        self._mqtt_publish_bool('button_lights/feature_enabled', feature_enabled)

    def _button_lights_enable_set(self, enable: bool):
        self._mqtt_publish_bool('button_lights/enable', enable)

    def _button_lights_aging_type_set(self, aging_type: AgingType):
        self._mqtt_publish_str('button_lights/aging_type', aging_type.name)

    #########################

    def _buttons_auto_lock_enable_set(self, enable: bool):
        self._mqtt_publish_bool('buttons_auto_lock/enable', enable)

    def _buttons_auto_lock_threshold_set(self, threshold: int):
        self._mqtt_publish_int('buttons_auto_lock/threshold', threshold)

    #########################

    def _feeding_video_enable(self, enable: bool):
        self._mqtt_publish_bool('feeding_video/enable', enable)

    def _feeding_video_on_feeding_plan_trigger_enable(self, enable: bool):
        self._mqtt_publish_bool('feeding_video/on_feeding_plan_trigger_enable', enable)

    def _feeding_video_on_manual_feeding_trigger_enable(self, enable: bool):
        self._mqtt_publish_bool('feeding_video/on_manual_feeding_trigger_enable', enable)

    def _feeding_video_time_before_feeding_plan_trigger(self, time: int):
        self._mqtt_publish_int('feeding_video/time_before_feeding_plan_trigger', time)

    def _feeding_video_time_after_manual_feeding_trigger(self, time: int):
        self._mqtt_publish_int('feeding_video/time_after_manual_feeding_trigger', time)

    def _feeding_video_time_automatic_recording(self, time: int):
        self._mqtt_publish_int('feeding_video/time_automatic_recording', time)

    def _feeding_video_watermark(self, enable: bool):
        self._mqtt_publish_bool('feeding_video/watermark', enable)

    #########################

    def _power_battery_level_set(self, battery_level: PercentInt):
        self._mqtt_publish_int('power/battery_level', battery_level.value_get())

    def _power_mode_set(self, mode: PowerMode):
        self._mqtt_publish_str('power/mode', mode.name)

    def _power_type_set(self, type_: PowerType):
        self._mqtt_publish_str('power/type', type_.name)

    #########################

    def _food_motor_state_set(self, motor_state: int):
        self._mqtt_publish_int('food/motor_state', motor_state)

    def _food_outlet_blocked_set(self, outlet_blocked: bool):
        self._mqtt_publish_bool('food/outlet_blocked', outlet_blocked)

    def _food_low_fill_level_set(self, low_fill_level: bool):
        self._mqtt_publish_bool('food/low_fill_level', low_fill_level)

    def _food_manual_feed_grain_num_set(self, grain_num: int):
        self._mqtt_publish_int('food/manual_feed_grain_num', grain_num)

    def _food_plans_set(self, food_plans: FoodPlans):
        for plan in food_plans.plans:
            topic = 'food/plan_{}'.format(plan.id_)
            self._mqtt_publish_dict(topic, plan.to_dict(), True)

    #########################

    def _food_output_progress_set(self, progress: FoodOutputProgress):
        self._mqtt_publish_str('food_output/progress', progress.name)

    def _food_output_last_start_set(self, last_start: datetime.datetime):
        self._mqtt_publish_datetime('food_output/last_start', last_start)

    def _food_output_last_end_set(self, last_end: datetime.datetime):
        self._mqtt_publish_datetime('food_output/last_end', last_end)

    def _food_output_last_grain_count_set(self, last_grain_count: int):
        self._mqtt_publish_int('food_output/last_grain_count', last_grain_count)

    def _food_output_last_trigger_set(self, last_trigger: GrainOutputType):
        self._mqtt_publish_str('food_output/last_trigger', last_trigger.name)

    ############################################################################

    def _mqtt_cmd_audio_enable_cb(self, eventname: str, data: dict, kwargs):
        self.backend.settings_audio(enable = self._mqtt_payload_boolean_to_bool(data['payload']))

    def _mqtt_cmd_audio_file_url_cb(self, eventname: str, data: dict, kwargs):
        self.backend.settings_audio(file_url = data['payload'])

    #########################

    def _mqtt_cmd_camera_enable_cb(self, eventname: str, data: dict, kwargs):
        self.backend.settings_camera(enable = self._mqtt_payload_boolean_to_bool(data['payload']))

    def _mqtt_cmd_camera_aging_type_cb(self, eventname: str, data: dict, kwargs):
        self.backend.settings_camera(aging_type = AgingType[data['payload']])

    def _mqtt_cmd_camera_night_vision_cb(self, eventname: str, data: dict, kwargs):
        self.backend.settings_camera(night_vision = NightVision[data['payload']])

    def _mqtt_cmd_camera_resolution_cb(self, eventname: str, data: dict, kwargs):
        self.backend.settings_camera(resolution = Resolution[data['payload']])

    #########################

    def _mqtt_cmd_recording_enable_cb(self, eventname: str, data: dict, kwargs):
        self.backend.settings_recording(enable = self._mqtt_payload_boolean_to_bool(data['payload']))

    def _mqtt_cmd_recording_aging_type_cb(self, eventname: str, data: dict, kwargs):
        self.backend.settings_recording(aging_type = AgingType[data['payload']])

    def _mqtt_cmd_recording_mode_cb(self, eventname: str, data: dict, kwargs):
        self.backend.settings_recording(mode = VideoRecordMode[data['payload']])

    #########################

    def _mqtt_cmd_motion_detection_enable_cb(self, eventname: str, data: dict, kwargs):
        self.backend.settings_motion_detection(enable = self._mqtt_payload_boolean_to_bool(data['payload']))

    def _mqtt_cmd_motion_detection_aging_type_cb(self, eventname: str, data: dict, kwargs):
        self.backend.settings_motion_detection(aging_type = AgingType[data['payload']])

    def _mqtt_cmd_motion_detection_range_cb(self, eventname: str, data: dict, kwargs):
        self.backend.settings_motion_detection(range_ = MotionDetectionRange[data['payload']])

    def _mqtt_cmd_motion_detection_sensitivity_cb(self, eventname: str, data: dict, kwargs):
        self.backend.settings_motion_detection(sensitivity = MotionDetectionSensitivity[data['payload']])

    #########################

    def _mqtt_cmd_sound_detection_enable_cb(self, eventname: str, data: dict, kwargs):
        self.backend.settings_sound_detection(enable = self._mqtt_payload_boolean_to_bool(data['payload']))

    def _mqtt_cmd_sound_detection_aging_type_cb(self, eventname: str, data: dict, kwargs):
        self.backend.settings_sound_detection(aging_type = AgingType[data['payload']])

    def _mqtt_cmd_sound_detection_sensitivity_cb(self, eventname: str, data: dict, kwargs):
        self.backend.settings_sound_detection(sensitivity = SoundDetectionSensitivity[data['payload']])

    #########################

    def _mqtt_cmd_feeding_video_enable_cb(self, eventname: str, data: dict, kwargs):
        self.backend.settings_feeding_video(enable = self._mqtt_payload_boolean_to_bool(data['payload']))

    def _mqtt_cmd_feeding_video_on_feeding_plan_trigger_enable_cb(self, eventname: str, data: dict, kwargs):
        self.backend.settings_feeding_video(video_on_start_feeding_plan = self._mqtt_payload_boolean_to_bool(data['payload']))

    def _mqtt_cmd_feeding_video_on_manual_feeding_trigger_enable_cb(self, eventname: str, data: dict, kwargs):
        self.backend.settings_feeding_video(video_after_manual_feeding = self._mqtt_payload_boolean_to_bool(data['payload']))

    def _mqtt_cmd_feeding_video_time_before_feeding_plan_trigger_cb(self, eventname: str, data: dict, kwargs):
        self.backend.settings_feeding_video(recording_length_before_feeding_plan_time = int(data['payload']))

    def _mqtt_cmd_feeding_video_time_after_manual_feeding_trigger_cb(self, eventname: str, data: dict, kwargs):
        self.backend.settings_feeding_video(recording_length_after_manual_feeding_time = int(data['payload']))

    def _mqtt_cmd_feeding_video_time_automatic_recording_cb(self, eventname: str, data: dict, kwargs):
        self.backend.settings_feeding_video(automatic_recording = int(data['payload']))

    def _mqtt_cmd_feeding_video_watermark_cb(self, eventname: str, data: dict, kwargs):
        self.backend.settings_feeding_video(video_watermark = self._mqtt_payload_boolean_to_bool(data['payload']))

    #########################

    def _mqtt_cmd_cloud_video_recording_enable_cb(self, eventname: str, data: dict, kwargs):
        self.backend.settings_cloud_video_recording(enable = self._mqtt_payload_boolean_to_bool(data['payload']))

    #########################

    def _mqtt_cmd_buttons_auto_lock_enable_cb(self, eventname: str, data: dict, kwargs):
        self.backend.settings_buttons_auto_lock(enable = self._mqtt_payload_boolean_to_bool(data['payload']))

    def _mqtt_cmd_buttons_auto_lock_threshold_cb(self, eventname: str, data: dict, kwargs):
        self.backend.settings_buttons_auto_lock(thresold = int(data['payload']))

    #########################

    def _mqtt_cmd_sound_enable_cb(self, eventname: str, data: dict, kwargs):
        self.backend.settings_sound(enable = self._mqtt_payload_boolean_to_bool(data['payload']))

    def _mqtt_cmd_sound_aging_type_cb(self, eventname: str, data: dict, kwargs):
        self.backend.settings_sound(aging_type = AgingType[data['payload']])

    def _mqtt_cmd_sound_volume_cb(self, eventname: str, data: dict, kwargs):
        self.backend.settings_sound(volume = PercentageInt(int(data['payload'])))

    #########################

    def _mqtt_cmd_button_light_enable_cb(self, eventname: str, data: dict, kwargs):
        self.backend.settings_button_lights(enable = self._mqtt_payload_boolean_to_bool(data['payload']))

    def _mqtt_cmd_button_light_aging_type_cb(self, eventname: str, data: dict, kwargs):
        self.backend.settings_button_lights(aging_type = AgingType[data['payload']])

    #########################

    def _mqtt_cmd_food_plans(self, eventname: str, data: dict, kwargs):
        try:
            payload = json.loads(data['payload'])
            food_plan = FoodPlan.from_dict(payload)
        except Exception as error:
            self.ad.log("WARNING: Invalid input for food plan payload, ignoring: {}".format(data['payload']))
            self.ad.log("Exception message: {}".format(error))
            return

        food_plans: FoodPlans = self.storage.food_plans_get()
        food_plans.plan_set(food_plan)
        self.storage.food_plans_set(food_plans)

        self.backend.food_plans_set(food_plans)

    def _mqtt_cmd_manual_feed_grain_num_cb(self, eventname: str, data: dict, kwargs):
        self.storage.food_manual_feed_grain_num_set(int(data['payload']))

    def _mqtt_cmd_manual_feed_cb(self, eventname: str, data: dict, kwargs):
        self.backend.food_manual_feed_now(self.storage.food_manual_feed_grain_num_get())

    #########################

    def _mqtt_cmd_device_reboot(self, eventname: str, data: dict, kwargs):
        self.backend.device_reboot()

    def _mqtt_cmd_device_factory_reset(self, eventname: str, data: dict, kwargs):
        self.backend.device_factory_reset()

    def _mqtt_cmd_device_wifi_reconnect(self, eventname: str, data: dict, kwargs):
        self.backend.device_wifi_reconnect()

    def _mqtt_cmd_device_sd_card_format(self, eventname: str, data: dict, kwargs):
        self.backend.device_sd_card_format()

    ############################################################################

    def _mqtt_payload_boolean_to_bool(self, payload_bool: str) -> bool:
        return True if payload_bool == 'true' else False

    def _mqtt_publish_str(self, topic: str, data: str, retain: bool = False):
        full_topic_path = self._topic_base_path_get(topic)
        self._mqtt_publish(full_topic_path, data, retain)

    def _mqtt_publish_int(self, topic: str, data: int, retain: bool = False):
        full_topic_path = self._topic_base_path_get(topic)
        self._mqtt_publish(full_topic_path, data, retain)

    def _mqtt_publish_bool(self, topic: str, data: bool, retain: bool = False):
        str = 'true' if data == True else 'false'
        full_topic_path = self._topic_base_path_get(topic)
        self._mqtt_publish(full_topic_path, str, retain)

    def _mqtt_publish_dict(self, topic: str, data: dict, retain: bool = False):
        full_topic_path = self._topic_base_path_get(topic)

        json_str = json.dumps(data)
        self._mqtt_publish(full_topic_path, json_str, retain)

    def _mqtt_publish_datetime(self, topic: str, data: datetime.datetime, retain: bool = False):
        datetime_utc = data.astimezone(datetime.timezone.utc)
        epoch_timestamp = int(datetime_utc.timestamp())

        full_topic_path = self._topic_base_path_get(topic)
        self._mqtt_publish(full_topic_path, epoch_timestamp, retain)

    def _mqtt_publish(self, topic: str, data: str, retain: bool = False):
        self.mqtt.mqtt_publish(topic, data, namespace = "mqtt", retain = retain)

    def _mqtt_subscribe(self, topic: str, callback):
        full_topic_path = self._topic_base_path_get(topic)

        self.mqtt.listen_event(callback, "MQTT_MESSAGE", topic = full_topic_path, namespace = "mqtt")

    def _mqtt_unsubscribe(self, topic: str):
        full_topic_path = self._topic_base_path_get(topic)

        self.mqtt.mqtt_unsubscribe(full_topic_path, namespace = 'mqtt')

    def _topic_base_path_get(self, topic: str):
        return "plaf203/{}/{}".format(self.serial_number, topic)