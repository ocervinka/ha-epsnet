from ..models import RuntimeChannelDefinition, RuntimeModelInfo

MODEL_INFO = RuntimeModelInfo(
    unit_code=0x0C35,
    manufacturer="ELKO EP / Teco",
    model="WSB2-20",
    type_key="generic_room_controller",
    subsystem="room_controls",
    description="2-button wall switch with LEDs and thermostat input",
    channels=(
        *(
            RuntimeChannelDefinition(
                role="button_input",
                name_template="button_{channel:02d}",
                channel_index=index + 1,
                register="X",
                storage="B",
                data_type="BOOL",
                address_offset=0,
                bit_offset=index,
            )
            for index in range(2)
        ),
        RuntimeChannelDefinition(
            role="temperature",
            name_template="temperature",
            channel_index=0,
            register="X",
            storage="F",
            data_type="REAL",
            address_offset=1,
        ),
        *(
            RuntimeChannelDefinition(
                role="led_state",
                name_template="led_{channel:02d}",
                channel_index=index + 1,
                register="Y",
                storage="B",
                data_type="BOOL",
                address_offset=0,
                bit_offset=index,
                writable=True,
            )
            for index in range(2)
        ),
    ),
)
