from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query
from sqlalchemy import select

from ..access import ActorDep
from ..errors import CommerceError
from ..models import Product
from ..schemas import ProductCreate, ProductUpdate, StockAdjustment, ZoneCreate, ZoneUpdate

router = APIRouter(prefix="/commerce/v1", tags=["catalog"])


@router.get("/products")
def list_products(
    actor: ActorDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0, le=100000)] = 0,
):
    return actor.commerce.list_products(limit=limit, offset=offset)


@router.post("/products", status_code=201)
def create_product(data: ProductCreate, actor: ActorDep):
    actor.require("owner", "supervisor")
    return actor.commerce.create_product(data.model_dump())


@router.get("/products/{product_id}")
def get_product(product_id: UUID, actor: ActorDep):
    return actor.commerce.get_product(product_id)


@router.patch("/products/{product_id}")
def update_product(product_id: UUID, data: ProductUpdate, actor: ActorDep):
    actor.require("owner", "supervisor")
    changes = data.model_dump(exclude_unset=True)
    expected = changes.pop("expected_version", None)
    if expected is not None:
        item = actor.session.scalar(
            select(Product)
            .where(Product.tenant_id == actor.tenant_id, Product.id == product_id)
            .with_for_update()
        )
        if item is None:
            raise CommerceError("not_found", "Product not found", 404)
        if item.version != expected:
            raise CommerceError("version_conflict", "Product changed; reload before editing")
    if any(value is None for key, value in changes.items() if key != "image_url"):
        raise CommerceError("invalid_product", "Product fields cannot be null", 422)
    return actor.commerce.update_product(product_id, changes)


@router.post("/products/{product_id}/stock")
def adjust_stock(product_id: UUID, data: StockAdjustment, actor: ActorDep):
    actor.require("owner", "supervisor")
    return actor.commerce.adjust_stock(product_id, data.delta)


@router.get("/delivery-zones")
def list_zones(actor: ActorDep):
    return actor.commerce.list_zones()


@router.post("/delivery-zones", status_code=201)
def create_zone(data: ZoneCreate, actor: ActorDep):
    actor.require("owner", "supervisor")
    return actor.commerce.create_zone(data.model_dump())


@router.patch("/delivery-zones/{zone_id}")
def update_zone(zone_id: UUID, data: ZoneUpdate, actor: ActorDep):
    actor.require("owner", "supervisor")
    changes = data.model_dump(exclude_unset=True)
    if any(value is None for value in changes.values()):
        raise CommerceError("invalid_zone", "Delivery fields cannot be null", 422)
    return actor.commerce.update_zone(zone_id, changes)
