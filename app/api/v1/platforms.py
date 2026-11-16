from fastapi import APIRouter

from app.platforms import registry
from app.schemas.config_schema import PlatformConfigSchema

router = APIRouter(prefix="/platforms", tags=["platforms"])

@router.get("")
async def list_platforms() -> dict[str, list[str]]:
    return {"platforms": registry.supported_platforms()}

@router.get("/{platform}/config-schema", response_model=PlatformConfigSchema)
async def get_platform_config_schema(platform: str) -> PlatformConfigSchema:
    return registry.get_adapter(platform).config_schema()
