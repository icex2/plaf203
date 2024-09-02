# Petlibro Automatic (Cat) Feeder (PLAF) AppDaemon integration/app

This is (currently) a **prototype** [AppDaemon](https://github.com/AppDaemon/appdaemon)-based
application to integrate
[Petlibro's Automatic Feeder](https://petlibro.com/products/petlibro-granary-automatic-pet-feeder-with-camera)
model *PLAF203S* into home assistant using MQTT. This enables users to control and monitor their
device locally without Petlibro's proprietary App and Cloud platform.

## Disclaimer and remarks

* Not everything has been implemented, yet
* Not everything has been fully and extensively tested
* Not everything is guaranteed to work/to be bug free
* I am not a python guy, please excuse any non-idiomatic patterns and practices
* Your device might not be compatible due to different hardware or software versions

I am releasing my research and code to the public so interested hackers and home enthusiasts can
pick it up and take it to the next level. It would make me glad to see this research serves as a
foundation for creating a proper home assistant integration for these devices.

The goal of this project is (currently) **NOT** to provide a fully working and battle tested 
integration for the device and it's different models and versions.

I am appreciating any comments/suggestions or contributions to this in the form of issues or
pull requests with bugfixes or improvements and I eventually look into these. However, I have
no intentions nor capacity to become a maintainer of this.

## License

Source code license is the Unlicense; you are permitted to do with this as thou wilt. For details,
please refer to the [LICENSE file](LICENSE) included with the source code.

## Status quo

All of my research and implementation is based on a `PLAF203S` device with:

* Hardware version: `1.0.7`
* Software version: `3.0.14`

### Done

* Full protocol reverse engineered and documented (minus the last unknown bits)
* MQTT discovery for home assistant
* NTP synchronization and drift detection
* Device life cycle handling and auto reconnect
* Manual feeding
* Food plans
* (Nearly) all configuration/feature switches, e.g. turn on/off camera, audio, sound, motion
  detection, feeding video recording features
* State of all features as sensors, e.g. SD card, power/battery backup, food output state
* Various diagnostic information about the device as sensors
* Device specific actions: reboot, wifi reconnect, factory reset, sd card format

### Not working/not implemented

* Age type "Schedule enable" and supporting fields on all features that support this. Currently only supported the
  default age type "non schedule enabled"
* OTA
* Anything non plaf203 related, e.g. some water fountain specific stuff
* Testing/supporting any other feeding devices similar to the PLAF203S
* Camera and audio streaming. Uses a [proprietary API with a audio/video cloud SaaS solution](#video-and-audio-streaming)
* Motion detection event dispatching + exposing as motion sensor
* Wifi reconfiguration

### Known issues/broken

* If setting audio url and the audio file cannot be downloaded (can be observed on the uart output console of the
  device), the device crashes and restarts
  * Mitigation: don't use the feature now and just disable feeding audio using the feature switch
* "Buttons auto lock" is not what I expected it to be. Currently, only the enable switch does something but I don't 
  know what. The threshold slider is currently broken
* Recording videos to SD card seems to work, but no idea how to open them

## Reverse engineering and research

### Differences tuya version (PLAF203) vs. mqtt version (PLAF203S)

Even the feeders look identical from the outside, the hardware and software stack are very 
different.

More information can be found on the
[home assistant forums](https://community.home-assistant.io/t/petlibro-cat-feeder/498637/34) already.

The key difference relevant to kicking off the reverse engineering efforts here were:

* Model identifier: The MQTT-based ones are PLAF203S models, see FCC-ID on the bottom of the device
  * [FCC report PLAF203](https://fcc.report/FCC-ID/2A3DE-PLAF203)
  * [FCC report PLAF203S](https://fcc.report/FCC-ID/2A3DE-PLAF203S)
* Different microcontroller and firmware, so
  [known methods for intrusion via scripts on the SD card](https://github.com/taylorfinnell/PLAF203-research) don't \
  seem to work anymore
* MQTT-based instead of local tuya

### Setup

#### Serial console and debug output

Luckily, the device has three pins on the PCB exposed and easily accessible which are `RXD`, `TXD`
and `GND` to hook up a serial console via a serial adapter USB adapter with the baudrate set to
`115200`. The device outputs all terminal output, from initial bootloaders, the kernel and userspace
to the serial console.

For example, using [minicom](https://linux.die.net/man/1/minicom):
`minicom --capturefile capture.dump  --baudrate 115200 --device /dev/tty.usbserial-130`

Which also writes all output to a file called `capture.dump` which was very useful to come back to
package dumps that I created while playing around with the official app.

Furthermore, the main application is also very verbose with a lot of useful debug output to 
understand and debug application and device behavior including full MQTT sent and received message
dumps.

Remark: I haven't been able to send keyboard inputs to the device which would have allowed me to
interrupt the boot process and probably use the shell that is spawned during the boot process.

#### Network connection

In order to get the device on-boarded and accessible on my local network, I used the official
petlibro app once to pair the device to my local wifi. Once that was finished, I blocked all access
to the outside for the device as that's not needed anymore for running it fully local (without
using the official app).

#### MQTT connection

* The device tries to connect to the MQTT server using the following DNS entries
  * `mqtt.us.petlibro.com`
  * `us-mqtt-0.aiotlibro.com`
  * `us-mqtt-0.dl-aiot.com`
* I set up my local DNS server to redirect `mqtt.us.petlibro.com` to my own local MQTT server instance
* The device tries to connect non-encrypted first on port `1883` which is somewhat questionable from
  a security perspective under intended usage, but useful in this case to get it connected easily on
  my local network
* The device tries to connect with a factory-configured product key, product secret and device ID
  to the MQTT instance
  * Either allow it to connect without authorization
  * Or have a look at the [serial console](#serial-console-and-debug-output) on a full boot cycle
    to dump the credentials as they are printed to the console in clear text. Try to find the
    log line looking like this:
    `MQTTConnect retry,DL_PRODUCT_KEY:<KEY>, DL_PRODUCT_SECRET:<SECRET>, DL_MQTT_ADDR:mqtt.us.petlibro.com, DL_DEVICE_ID:<ID>`
    * `DL_PRODUCT_KEY` = username
    * `DL_PRODUCT_SECRET` = password
    * `DL_DEVICE_ID` will be the name of the device in mqtt

#### AppDaemon integration configuration

The current project structure is kept as a simple "mono-file" which makes it fairly straight forward
to deploy it to AppDaemon:

* Copy the `plaf203.py` file to your `appdaemon/apps` folder
* Copy the section of `apps.yaml` to your AppDaemon installation's `apps.yaml`
  * Replace `serial_number` with your device's serial number which is the `DL_DEVICE_ID` you
    [need to acquire somehow](#mqtt-connection)
  * Replace the `mqtt_host` with either the hostname or IP of your MQTT broker
* Check the AppDaemon logs if everything starts up fine, you should see some log output from the
  integration similar to this: `INFO plaf203: Initializing plaf203, serial number 00000000000000000`
* Turn your device on, make sure it connects fine to your MQTT broker, check the
  [serial console output](#serial-console-and-debug-output) and if topics starting with
  `dl/plaf203/...` are created on your broker
* Considering you have MQTT discover with home assistant enabled, the device should appear on your
  home assistant's MQTT integration as `Pet libro cat feeder`

### Some more network communication

Capture the network traffic and also looking into the firmware, I discovered the following hostnames
and IP addresses that are either used or potentially used:

* MQTT
  * `mqtt.us.petlibro.com`
  * `us-mqtt-0.aiotlibro.com`
  * `us-mqtt-0.dl-aiot.com`
* IPs
  * `54.156.99.57` -> Petlibro MQTT on AWS in US
  * `44.211.92.174` -> tutk ip?
  * `139.162.174.232` -> Akamai? Maybe for the feeding audio asset(s) 
* Extracted URLs from firmware binary
  * `mqtt.us.petlibro.com`:1883
  * `sit-svc.dl-aiot.com`:1883
  * `demo-svc.dl-aiot.com`:1883
  * `mqtt.dl-aiot.com`:1883
  * `test.svc.dl-aiot.com`:1883
  * `kalay.net.cn`
  * `kalayservice.com`
  * `iotcplatform.com`

### The "pet feeder procotol" over MQTT

* There are two directions of request-response communication
  * Backend/server -> device
    * The server posts MQTT messages to a topic with path ending in `sub`
    * Example: `dl/plaf203/00000000000000000/device/service/sub`
    * The device is expected to subscribe to these topics and consume the
      messages
    * The backend consumes any responses by the device from the same 
      endpoint/path, but ending in `post`
    * Example: `dl/plaf203/00000000000000000/device/service/post`
  * Device -> backend/server
    * The device posts MQTT message to the topic with the path ending in `post`
    * Example: `dl/plaf203/00000000000000000/device/event/post`
    * The backend is expected to subscribe to these topics and consume the
      messages
    * The device consumes any responses by the backend from the same
      endpoint/path, but ending in `sub`
    * Example: `dl/plaf203/00000000000000000/device/event/sub`
* Communication is further grouped with topics by "types of communication"
  * `heart`: Heartbeat messages from device
  * `ota`: Over the air firmware updates
  * `ntp`: NTP time synchronization for device
  * `broadcast`: Some currently unknown broadcast channel?
  * `config`: Device/system configuration related, direction: server -> device
  * `event`: Main channel for feeder product features communication, direction: device -> server
  * `service`: Main channel for feeder product features communication, direction: server -> device
  * `system`: System related commands: direction: server -> device
* Messages sent over these topics/channels always (except for heartbeat and ntp) have the following
  header information:
  * `cmd`: The command/message type identifier (see `plaf203.py` at the top for all commands and
    documentation)
  * `message_id`: Unique identifier for messages, IDs of responses have to match requests
  * `timestamp`: Unix-epoch timestamp in milliseconds (UTC)

### Video and audio streaming

* The device uses some API referred to as `tutk` in the firmware which is the Kalay SDK of ThroughTek
  * [API reference](https://github.com/taishanmayi/tutk_test/blob/master/include/AVAPIs.h)
* The API integrates with a SaaS video streaming platform by [Throughtek](https://www.throughtek.com/overview/)
* Some more potentially useful code references
    * https://github.com/taishanmayi/tutk_test/tree/master
    * https://github.com/TutkKalay/Kalay_Kit_Sample_App/tree/master

No actual work has been done on this. It seems to require a re-implementation of the server to
talk to the client portion of the device correctly in order to implement local video streaming.

Maybe there is an easier solution to modify the system once it is possible to access it.

### Feeding audio playback

* The device supports playing a pre-recorded audio track when outputting food
* [Default audio file url](https://dl-oss-prod.s3.us-east-1.amazonaws.com/platform/audio/come_to_eat.aac)
* The device tries to download the audio file and will crash and burn (i.e. reboot) if it fails to
  do so without any further error output to the user