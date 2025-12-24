"""Модуль с Pydantic схемами для API."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import AnyHttpUrl, BaseModel, Field, validator


class PyObjectId(str):
    """PyObjectId модель."""

    @classmethod
    def __get_validators__(cls):
        """Получает валидаторы для Pydantic."""
        yield cls.validate

    @classmethod
    def validate(cls, v):
        """Валидирует значение как ObjectId."""
        from bson import ObjectId

        if isinstance(v, ObjectId):
            return str(v)
        if isinstance(v, str) and ObjectId.is_valid(v):
            return v
        raise ValueError("Invalid ObjectId")


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

    product_id: str
    variant_id: Optional[str] = None
    quantity: int = Field(..., ge=1, le=50)


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
    PROCESSING = "в обработке"
    ACCEPTED = "принят"
    SHIPPED = "выехал"
    DONE = "завершён"
    CANCELED = "отменён"


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
    status: OrderStatus = OrderStatus.NEW
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
    sleep_until: Optional[datetime] = None
    payment_link: Optional[str] = None
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class StoreSleepRequest(BaseModel):
    """StoreSleepRequest модель."""

    sleep: bool
    message: Optional[str] = None
    sleep_until: Optional[datetime] = None


class PaymentLinkRequest(BaseModel):
    """PaymentLinkRequest модель."""

    url: Optional[AnyHttpUrl] = Field(
        default=None,
        description="Ссылка на страницу оплаты (например, Kaspi Pay)",
    )


class PaginatedOrdersResponse(BaseModel):
    """PaginatedOrdersResponse модель."""

    orders: List[Order]
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
