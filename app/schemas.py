"""Модуль с Pydantic схемами для API."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List, Optional

from bson import ObjectId
from pydantic import AnyHttpUrl, BaseModel, Field
from pydantic_core import core_schema


class PyObjectId(str):
    """PyObjectId модель для Pydantic v2."""

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type, handler
    ):
        """Получает схему для Pydantic v2."""
        def validate_value(v):
            """Валидирует значение как ObjectId."""
            if isinstance(v, ObjectId):
                return str(v)
            if isinstance(v, str):
                if ObjectId.is_valid(v):
                    return v
                raise ValueError(f"Invalid ObjectId: {v}")
            raise ValueError(f"Invalid ObjectId type: {type(v)}")

        from_str_schema = core_schema.chain_schema(
            [
                core_schema.str_schema(),
                core_schema.no_info_plain_validator_function(validate_value),
            ]
        )

        return core_schema.json_or_python_schema(
            json_schema=from_str_schema,
            python_schema=core_schema.union_schema(
                [
                    core_schema.is_instance_schema(ObjectId),
                    from_str_schema,
                ]
            ),
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda x: str(x) if isinstance(x, ObjectId) else x
            ),
        )


class CategoryBase(BaseModel):
    """CategoryBase модель."""

    name: str = Field(..., min_length=1, max_length=64)


class CategoryCreate(CategoryBase):
    """CategoryCreate модель."""

    pass


class CategoryUpdate(BaseModel):
    """CategoryUpdate модель."""

    name: Optional[str] = Field(None, min_length=1, max_length=64)


class Category(CategoryBase):
    """Category модель."""

    id: PyObjectId = Field(default_factory=PyObjectId, alias="id")

    class Config:
        """Конфигурация Pydantic."""

        populate_by_name = True
        extra = "allow"  # Разрешаем дополнительные поля из базы данных


class ProductBase(BaseModel):
    """ProductBase модель."""

    name: str
    description: Optional[str] = None
    price: float = Field(..., ge=0)
    image: Optional[str] = None
    images: Optional[List[str]] = None
    category_id: str
    available: bool = True


class ProductCreate(ProductBase):
    """ProductCreate модель."""

    variants: Optional[List[dict]] = None


class ProductUpdate(BaseModel):
    """ProductUpdate модель."""

    name: Optional[str] = None
    description: Optional[str] = None
    price: Optional[float] = Field(None, ge=0)
    image: Optional[str] = None
    images: Optional[List[str]] = None
    category_id: Optional[str] = None
    available: Optional[bool] = None
    variants: Optional[List[dict]] = None


class Product(ProductBase):
    """Product модель."""

    id: PyObjectId = Field(default_factory=PyObjectId, alias="id")
    variants: Optional[List[dict]] = None

    class Config:
        """Конфигурация Pydantic."""

        populate_by_name = True
        extra = "allow"  # Разрешаем дополнительные поля из базы данных


class CatalogResponse(BaseModel):
    """CatalogResponse модель."""

    categories: List[Category]
    products: List[Product]


class CartItem(BaseModel):
    """CartItem модель."""

    id: str
    product_id: str
    product_name: str
    quantity: int = Field(..., ge=1)
    price: float
    image: Optional[str] = None
    variant_id: Optional[str] = None
    variant_name: Optional[str] = None


class Cart(BaseModel):
    """Cart модель."""

    id: PyObjectId = Field(default_factory=PyObjectId, alias="id")
    user_id: int
    items: List[CartItem] = Field(default_factory=list)
    total_amount: float = 0.0

    class Config:
        """Конфигурация Pydantic."""

        populate_by_name = True


class AddToCartRequest(BaseModel):
    """AddToCartRequest модель."""

    product_id: str = Field(..., min_length=1, description="ID товара")
    variant_id: str = Field(..., min_length=1, description="ID вариации (вкуса) товара")
    quantity: int = Field(..., ge=1, le=50, description="Количество товара")


class RemoveFromCartRequest(BaseModel):
    """RemoveFromCartRequest модель."""

    item_id: str


class UpdateCartItemRequest(BaseModel):
    """UpdateCartItemRequest модель."""

    item_id: str
    quantity: int = Field(..., ge=1, le=50)


class OrderStatus(str, Enum):
    """OrderStatus модель."""

    NEW = "новый"
    ACCEPTED = "принят"
    REJECTED = "отказано"


class OrderItem(BaseModel):
    """OrderItem модель."""

    id: Optional[str] = None  # ID элемента корзины (для совместимости)
    product_id: str
    product_name: str
    quantity: int
    price: float
    image: Optional[str] = None  # Изображение товара
    variant_id: Optional[str] = None  # ID вариации (вкуса)
    variant_name: Optional[str] = None  # Название вариации (вкуса)


class Order(BaseModel):
    """Order модель."""

    id: PyObjectId = Field(default_factory=PyObjectId, alias="id")
    user_id: int
    customer_name: str
    customer_phone: str
    delivery_address: str
    comment: Optional[str] = None
    status: OrderStatus = OrderStatus.ACCEPTED
    rejection_reason: Optional[str] = None  # Причина отказа (если статус "отказано")
    items: List[OrderItem]
    total_amount: float
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    can_edit_address: bool = True
    payment_receipt_file_id: Optional[str] = None  # ID файла в GridFS
    payment_receipt_url: Optional[str] = None  # Устаревшее поле, оставлено для обратной совместимости
    payment_receipt_filename: Optional[str] = None
    delivery_type: Optional[str] = None
    payment_type: Optional[str] = None

    class Config:
        """Конфигурация Pydantic."""

        populate_by_name = True
        extra = "allow"  # Разрешаем дополнительные поля из базы данных


class CreateOrderRequest(BaseModel):
    """CreateOrderRequest модель."""

    name: str
    phone: str
    address: str
    comment: Optional[str] = None


class UpdateAddressRequest(BaseModel):
    """UpdateAddressRequest модель."""

    address: str


class UpdateStatusRequest(BaseModel):
    """UpdateStatusRequest модель."""

    status: OrderStatus
    rejection_reason: Optional[str] = None  # Причина отказа (обязательна для статуса "отказано")


class BroadcastRequest(BaseModel):
    """BroadcastRequest модель."""

    title: str
    message: str
    segment: str = Field(default="all")
    link: Optional[str] = None


class BroadcastResponse(BaseModel):
    """BroadcastResponse модель."""

    success: bool
    sent_count: int = 0
    total_count: int = 0
    failed_count: int = 0


class StoreStatus(BaseModel):
    """StoreStatus модель."""

    is_sleep_mode: bool = False
    sleep_message: Optional[str] = None
    # sleep_until и payment_link убраны, т.к. не используются


class StoreSleepRequest(BaseModel):
    """StoreSleepRequest модель."""

    sleep: bool
    message: Optional[str] = None
    # sleep_until убран, т.к. не используется


# PaymentLinkRequest удален, т.к. payment_link больше не используется


class OrderSummary(BaseModel):
    """Упрощенная модель заказа для списка (без полных items и лишних полей)."""

    id: PyObjectId = Field(default_factory=PyObjectId, alias="id")
    customer_name: str
    customer_phone: str
    delivery_address: str
    status: OrderStatus = OrderStatus.ACCEPTED
    total_amount: float
    created_at: datetime = Field(default_factory=datetime.utcnow)
    items_count: int = Field(..., description="Количество товаров в заказе")

    class Config:
        """Конфигурация Pydantic."""

        populate_by_name = True
        extra = "allow"


class PaginatedOrdersResponse(BaseModel):
    """PaginatedOrdersResponse модель."""

    orders: List[OrderSummary]
    next_cursor: Optional[str] = None


class CategoryDetail(BaseModel):
    """Детали категории с товарами."""

    category: Category
    products: List[Product]


class Customer(BaseModel):
    """Модель клиента."""

    id: PyObjectId = Field(default_factory=PyObjectId, alias="id")
    telegram_id: int
    added_at: datetime = Field(default_factory=datetime.utcnow)
    last_cart_activity: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        """Конфигурация Pydantic."""

        populate_by_name = True
