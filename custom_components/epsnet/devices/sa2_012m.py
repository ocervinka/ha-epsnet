from ..models import RuntimeChannelDefinition, RuntimeModelInfo

MODEL_INFO = RuntimeModelInfo(
    unit_code=0x0C4F,
    manufacturer="ELKO EP / Teco",
    model="SA2-012M",
    type_key="generic_relay_module",
    subsystem="relay_outputs",
    description="12-channel relay actuator",
    channels=(
        *(
            RuntimeChannelDefinition(
                role="local_switch",
                name_template="local_switch_{channel:02d}",
                channel_index=index + 1,
                register="X",
                storage="B",
                data_type="BOOL",
                address_offset=index // 8,
                bit_offset=index % 8,
            )
            for index in range(12)
        ),
        *(
            RuntimeChannelDefinition(
                role="channel_state",
                name_template="relay_{channel:02d}",
                channel_index=index + 1,
                register="Y",
                storage="B",
                data_type="BOOL",
                address_offset=index // 8,
                bit_offset=index % 8,
                writable=True,
            )
            for index in range(12)
        ),
    ),
)
