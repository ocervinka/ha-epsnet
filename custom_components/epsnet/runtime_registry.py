"""Runtime unit-code registry for EPSNET device discovery."""

from __future__ import annotations

from .devices.c_it_0200i import MODEL_INFO as C_IT_0200I
from .devices.da2_22m import MODEL_INFO as DA2_22M
from .devices.im2_140m import MODEL_INFO as IM2_140M
from .devices.im2_40b import MODEL_INFO as IM2_40B
from .devices.sa2_012m import MODEL_INFO as SA2_012M
from .devices.sa2_04m_sn import MODEL_INFO as SA2_04M_SN
from .devices.wsb2_20 import MODEL_INFO as WSB2_20
from .devices.wsb2_40 import MODEL_INFO as WSB2_40
from .models import RuntimeModelInfo

_RUNTIME_MODEL_INFOS = (
    C_IT_0200I,
    DA2_22M,
    IM2_140M,
    IM2_40B,
    SA2_012M,
    SA2_04M_SN,
    WSB2_20,
    WSB2_40,
)

RUNTIME_MODEL_INFOS_BY_CODE = {
    info.unit_code: info for info in _RUNTIME_MODEL_INFOS
}


def lookup_runtime_model_info(unit_code: int) -> RuntimeModelInfo:
    """Resolve one runtime unit code to manufacturer metadata."""
    if unit_code in RUNTIME_MODEL_INFOS_BY_CODE:
        return RUNTIME_MODEL_INFOS_BY_CODE[unit_code]
    
    return RuntimeModelInfo(
        unit_code=unit_code,
        manufacturer="Generic / Unknown",
        model=f"Unknown 0x{unit_code:04x}",
        type_key="generic_input_module", # Default to inputs for safety
        subsystem="digital_inputs",
        description=f"Unknown EPSNET device (0x{unit_code:04x})",
        channels=(), # No channels defined yet
    )
