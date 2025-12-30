"""Модуль для работы с каталогом товаров."""

import asyncio
import json
import logging
from datetime import datetime
from hashlib import sha256
from typing import List, Optional, Sequence, Tuple

from bson import ObjectId
from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument

from ..auth import verify_admin
from ..database import get_db

# Используем orjson если доступен, иначе fallback на стандартный json
try:
    import orjson

    HAS_ORJSON = True
except ImportError:
    import json as orjson

    HAS_ORJSON = False
from ..schemas import (
    CatalogResponse,
    Category,
    CategoryCreate,
    CategoryDetail,
    CategoryUpdate,
    Product,
    ProductCreate,
    ProductUpdate,
)
from ..utils import (
    as_object_id,
    delete_product_images_from_gridfs,
    get_gridfs,
    normalize_product_images,
    save_base64_image_to_gridfs,
    save_base64_images_to_gridfs,
    serialize_doc,
)

router = APIRouter(tags=["catalog"])
logger = logging.getLogger(__name__)


async def _load_catalog_from_db(db: AsyncIOMotorDatabase, only_available: bool = True) -> CatalogResponse:
    """
    Загружает каталог из БД.

    Args:
    db: Подключение к БД
    only_available: Загружать только доступные товары (по умолчанию True для оптимизации)
    """
    # Параллельная загрузка категорий и товаров для ускорения
    # Используем проекцию для уменьшения объема данных
    categories_task = db.categories.find({}, {"name": 1, "_id": 1}).to_list(length=None)

    # Фильтруем только доступные товары для публичного каталога (оптимизация)
    # Используем индекс для быстрой фильтрации
    products_filter = {"available": True} if only_available else {}
    products_task = (
        db.products.find(
            products_filter,
            {
                "name": 1,
                "description": 1,
                "price": 1,
                "image": 1,
                "images": 1,
                "category_id": 1,
                "available": 1,
                "variants": 1,
                "_id": 1,  # Явно включаем _id для консистентности
            },
        )
        # Используем составной индекс и сразу выгружаем в список, чтобы не передавать курсор в gather
        .hint([("category_id", 1), ("available", 1)]).to_list(length=None)
    )

    # Выполняем запросы параллельно
    categories_docs, products_docs = await asyncio.gather(categories_task, products_task)

    # Оптимизированная валидация категорий (без try-catch для скорости)
    categories = []
    for doc in categories_docs:
        name = doc.get("name")
        if not name or not isinstance(name, str):
            continue
        # Прямое создание без лишних проверок
        categories.append(Category(name=name, id=str(doc["_id"])))

    # Оптимизированная валидация товаров (минимальные проверки для скорости)
    products = []
    for doc in products_docs:
        # Быстрая предварительная проверка обязательных полей
        name = doc.get("name")
        if not name or not isinstance(name, str):
            continue

        category_id = doc.get("category_id")
        if not category_id:
            continue

        # Быстрая обработка цены
        price = doc.get("price", 0.0)
        if not isinstance(price, (int, float)):
            price = float(price) if price else 0.0

        # Собираем данные товара (минимальная валидация)
        product_data: dict = {
            "id": str(doc["_id"]),
            "name": name,
            "price": price,
            "category_id": str(category_id) if not isinstance(category_id, str) else category_id,
            "available": bool(doc.get("available", True)),
        }

        # Опциональные поля добавляем только если они есть
        if "description" in doc and doc["description"]:
            desc = doc["description"]
            product_data["description"] = desc[:300] if isinstance(desc, str) and len(desc) > 300 else desc
        
        # Нормализуем изображения: объединяем image и images в единый массив images
        normalized_doc = normalize_product_images(doc)
        if "images" in normalized_doc:
            product_data["images"] = normalized_doc["images"]
        if "image" in normalized_doc:
            product_data["image"] = normalized_doc["image"]
        
        if "variants" in doc:
            product_data["variants"] = doc["variants"]

        # Прямое создание без try-catch для скорости
        try:
            products.append(Product(**product_data))
        except Exception:
            # Пропускаем некорректные товары без логирования в production
            continue

    return CatalogResponse(categories=categories, products=products)


def _catalog_to_dict(payload: CatalogResponse) -> dict:
    # Используем exclude_unset для исключения None значений и уменьшения размера ответа
    return payload.dict(by_alias=True, exclude_none=False)


def _compute_catalog_etag(payload: CatalogResponse) -> str:
    payload_dict = _catalog_to_dict(payload)
    serialized = json.dumps(payload_dict, sort_keys=True, ensure_ascii=False)
    return sha256(serialized.encode("utf-8")).hexdigest()


def _empty_catalog() -> CatalogResponse:
    """Создает пустой каталог для fallback ответов."""
    return CatalogResponse(categories=[], products=[])


async def fetch_catalog(
    db: Optional[AsyncIOMotorDatabase],
    *,
    only_available: bool = True,
) -> Tuple[CatalogResponse, str]:
    """Загружает каталог из БД (без кэширования для актуальности данных)."""
    # Если БД недоступна, возвращаем пустой каталог
    if db is None:
        return _empty_catalog(), "empty-catalog"

    # Загружаем данные напрямую из БД без кэширования
    try:
        data = await _load_catalog_from_db(db, only_available=only_available)
        etag = _compute_catalog_etag(data)
        return data, etag
    except Exception as e:
        logger.error(f"Ошибка при загрузке каталога из БД: {e}", exc_info=True)
        return _empty_catalog(), "error-catalog"


def _build_catalog_response(catalog: CatalogResponse, etag: str) -> Response:
    """Создает ответ с использованием orjson/json для быстрой сериализации."""
    catalog_dict = _catalog_to_dict(catalog)
    # Используем orjson если доступен, иначе стандартный json
    if HAS_ORJSON:
        content = orjson.dumps(catalog_dict, option=orjson.OPT_SERIALIZE_NUMPY)
    else:
        content = orjson.dumps(catalog_dict).encode("utf-8")
    response = Response(
        content=content,
        media_type="application/json",
        headers={
            "ETag": etag,
            "Cache-Control": _build_cache_control_value(),
        },
    )
    return response


def _build_not_modified_response(etag: str) -> Response:
    headers = {
        "ETag": etag,
        "Cache-Control": _build_cache_control_value(),
    }
    return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)


def _build_cache_control_value() -> str:
    """
    Возвращает значение Cache-Control для каталога.

    Каталог меняется по требованию админа, поэтому клиентам нужно
    всегда перепроверять данные у API, даже если запросы идут подряд.
    Сервер всё равно держит тёплый кэш в памяти (_catalog_cache), поэтому
    повторные проверки практически не нагружают базу.
    Используем max-age=0 + must-revalidate, чтобы браузеры не возвращали
    устаревший ответ из собственного HTTP-кэша (причина исчезающих категорий).
    """
    return "public, max-age=0, must-revalidate"


@router.get("/catalog", response_model=CatalogResponse)
async def get_catalog(
    db: Optional[AsyncIOMotorDatabase] = Depends(get_db),
    if_none_match: str | None = Header(None, alias="If-None-Match"),
):
    """Возвращает каталог товаров (прямой запрос к БД без кэширования для актуальности данных)."""
    try:
        # Загружаем каталог напрямую из БД без кэширования
        catalog, etag = await fetch_catalog(db, only_available=True)

        if if_none_match and if_none_match == etag:
            return _build_not_modified_response(etag)
        return _build_catalog_response(catalog, etag)
    except HTTPException as e:
        # Если БД недоступна, возвращаем пустой каталог вместо ошибки
        if e.status_code == status.HTTP_503_SERVICE_UNAVAILABLE:
            return _build_catalog_response(_empty_catalog(), "empty-catalog")
        logger.error(f"HTTPException при получении каталога: {e.status_code} - {e.detail}")
        raise
    except Exception as e:
        logger.error(f"Ошибка при получении каталога: {type(e).__name__}: {e}", exc_info=True)
        # Возвращаем пустой каталог вместо 500, чтобы фронтенд не падал
        return _build_catalog_response(_empty_catalog(), "error-catalog")


@router.get("/admin/catalog", response_model=CatalogResponse)
async def get_admin_catalog(
    db: AsyncIOMotorDatabase = Depends(get_db),
    _admin_id: int = Depends(verify_admin),
):
    """
    Возвращает актуальный каталог для админки (прямой запрос к БД без кэширования).
    """
    try:
        # Админка загружает все товары, включая недоступные
        catalog, etag = await fetch_catalog(db, only_available=False)
        response = _build_catalog_response(catalog, etag)
        # Админке всегда нужен свежий ответ, поэтому блокируем клиентский кэш.
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response
    except Exception as e:
        import logging

        logger = logging.getLogger(__name__)
        logger.error(f"Ошибка при загрузке каталога для админки: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Ошибка при загрузке каталога: {str(e)}"
        )


@router.get("/admin/category/{category_id}", response_model=CategoryDetail)
async def get_admin_category_detail(
    category_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    _admin_id: int = Depends(verify_admin),
):
    """Возвращает детали категории для админки."""
    # Используем проекцию для минимизации данных
    category_doc = await db.categories.find_one(
        {"_id": {"$in": _build_id_candidates(category_id)}},
        {"name": 1, "_id": 1}
    )
    if not category_doc:
        raise HTTPException(status_code=404, detail="Категория не найдена")

    candidate_values = set(_build_id_candidates(category_id))
    if category_doc.get("_id"):
        candidate_values.add(str(category_doc["_id"]))

    # Используем проекцию для минимизации загружаемых данных
    products_cursor = db.products.find(
        {"category_id": {"$in": list(candidate_values)}},
        {
            "name": 1,
            "description": 1,
            "price": 1,
            "image": 1,
            "images": 1,
            "category_id": 1,
            "available": 1,
            "variants": 1,
            "_id": 1,
        }
    )
    products_docs = await products_cursor.to_list(length=None)

    serialized_category = serialize_doc(category_doc)
    serialized_category.pop("_id", None)  # Удаляем _id, так как используем id
    category_model = Category(**serialized_category | {"id": str(category_doc["_id"])})
    products_models = []
    for doc in products_docs:
        try:
            # Нормализуем изображения перед сериализацией
            normalized_doc = normalize_product_images(doc)
            serialized = serialize_doc(normalized_doc)
            serialized.pop("_id", None)  # Удаляем _id, так как используем id
            products_models.append(Product(**serialized | {"id": str(doc["_id"])}))
        except Exception:
            continue

    return CategoryDetail(category=category_model, products=products_models)


def _build_id_candidates(raw_id: str) -> Sequence[object]:
    candidates: set[object] = {raw_id}
    if ObjectId.is_valid(raw_id):
        oid = ObjectId(raw_id)
        candidates.add(oid)
        candidates.add(str(oid))
    return list(candidates)


@router.post(
    "/admin/category",
    response_model=Category,
    status_code=status.HTTP_201_CREATED,
)
async def create_category(
    payload: CategoryCreate,
    db: AsyncIOMotorDatabase = Depends(get_db),
    _admin_id: int = Depends(verify_admin),
):
    """Создает новую категорию."""
    if not payload.name or not payload.name.strip():
        raise HTTPException(status_code=400, detail="Название категории не может быть пустым")

    # Проверяем существование с проекцией для минимизации данных
    existing = await db.categories.find_one({"name": payload.name.strip()}, {"_id": 1})
    if existing:
        raise HTTPException(status_code=400, detail="Категория уже существует")

    category_data = {"name": payload.name.strip()}
    result = await db.categories.insert_one(category_data)
    # Используем проекцию для минимизации данных
    doc = await db.categories.find_one({"_id": result.inserted_id}, {"name": 1, "_id": 1})
    if not doc:
        raise HTTPException(status_code=500, detail="Ошибка при создании категории")
    
    serialized = serialize_doc(doc)
    serialized.pop("_id", None)  # Удаляем _id, так как используем id
    category = Category(**serialized | {"id": str(doc["_id"])})
    return category


@router.patch("/admin/category/{category_id}", response_model=Category)
async def update_category(
    category_id: str,
    payload: CategoryUpdate,
    db: AsyncIOMotorDatabase = Depends(get_db),
    _admin_id: int = Depends(verify_admin),
):
    """Обновляет категорию."""
    update_data = payload.dict(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="Нет данных для обновления")

    category_doc = await db.categories.find_one({"_id": {"$in": _build_id_candidates(category_id)}})
    if not category_doc:
        raise HTTPException(status_code=404, detail="Категория не найдена")

    if "name" in update_data and update_data["name"] is not None:
        update_data["name"] = update_data["name"].strip()
        if not update_data["name"]:
            raise HTTPException(status_code=400, detail="Название категории не может быть пустым")

        # Проверяем существование с проекцией для минимизации данных
        existing = await db.categories.find_one(
            {
                "name": update_data["name"],
                "_id": {"$ne": category_doc["_id"]},
            },
            {"_id": 1}
        )
        if existing:
            raise HTTPException(status_code=400, detail="Категория с таким названием уже существует")

    # Используем проекцию для минимизации данных
    result = await db.categories.find_one_and_update(
        {"_id": category_doc["_id"]},
        {"$set": update_data},
        return_document=ReturnDocument.AFTER,
        projection={"name": 1, "_id": 1}
    )
    if not result:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    
    serialized = serialize_doc(result)
    serialized.pop("_id", None)  # Удаляем _id, так как используем id
    category = Category(**serialized | {"id": str(result["_id"])})
    return category


@router.delete(
    "/admin/category/{category_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_category(
    category_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    _admin_id: int = Depends(verify_admin),
):
    """Удаляет категорию и все связанные товары с их изображениями из GridFS."""
    category_doc = await db.categories.find_one({"_id": {"$in": _build_id_candidates(category_id)}})
    if not category_doc:
        raise HTTPException(status_code=404, detail="Категория не найдена")

    cleanup_values: set[object] = {
        category_id,
        str(category_doc["_id"]),
    }
    if isinstance(category_doc["_id"], ObjectId):
        cleanup_values.add(category_doc["_id"])

    # Получаем все товары категории с их изображениями перед удалением
    products = await db.products.find(
        {"category_id": {"$in": list(cleanup_values)}},
        {"image": 1, "images": 1}
    ).to_list(length=None)
    
    # Удаляем изображения всех товаров из GridFS
    for product_doc in products:
        await delete_product_images_from_gridfs(product_doc)
    
    # Удаляем все товары категории
    await db.products.delete_many({"category_id": {"$in": list(cleanup_values)}})

    # Удаляем саму категорию
    delete_result = await db.categories.delete_one({"_id": category_doc["_id"]})
    if delete_result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Категория не найдена")

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/admin/product",
    response_model=Product,
    status_code=status.HTTP_201_CREATED,
)
async def create_product(
    payload: ProductCreate,
    db: AsyncIOMotorDatabase = Depends(get_db),
    _admin_id: int = Depends(verify_admin),
):
    """Создает новый товар."""
    # Проверяем категорию с проекцией для минимизации данных
    category = await db.categories.find_one({"_id": as_object_id(payload.category_id)}, {"_id": 1})
    if not category:
        raise HTTPException(status_code=400, detail="Категория не найдена")
    
    data = payload.dict()
    
    # Конвертируем base64 изображения в GridFS file_id
    if data.get("image") and isinstance(data["image"], str) and data["image"].startswith("data:image"):
        # Это base64 изображение, сохраняем в GridFS
        image_file_id = await save_base64_image_to_gridfs(data["image"])
        if image_file_id:
            data["image"] = image_file_id
        else:
            # Если не удалось сохранить, удаляем поле
            data.pop("image", None)
    
    if data.get("images") and isinstance(data["images"], list):
        # Конвертируем список base64 изображений
        image_ids = await save_base64_images_to_gridfs(data["images"])
        if image_ids:
            data["images"] = image_ids
        else:
            data.pop("images", None)
    
    # Нормализуем изображения: синхронизируем image и images
    data = normalize_product_images(data)
    
    result = await db.products.insert_one(data)
    # Используем проекцию для минимизации загружаемых данных
    doc = await db.products.find_one(
        {"_id": result.inserted_id},
        {
            "name": 1,
            "description": 1,
            "price": 1,
            "image": 1,
            "images": 1,
            "category_id": 1,
            "available": 1,
            "variants": 1,
            "_id": 1,
        }
    )
    # Нормализуем изображения перед сериализацией (на случай если данные уже были в БД)
    normalized_doc = normalize_product_images(doc)
    serialized = serialize_doc(normalized_doc)
    serialized.pop("_id", None)  # Удаляем _id, так как используем id
    product = Product(**serialized | {"id": str(doc["_id"])})
    return product


@router.patch("/admin/product/{product_id}", response_model=Product)
async def update_product(
    product_id: str,
    payload: ProductUpdate,
    db: AsyncIOMotorDatabase = Depends(get_db),
    _admin_id: int = Depends(verify_admin),
):
    """Обновляет товар."""
    update_payload = payload.dict(exclude_unset=True)
    product_oid = as_object_id(product_id)
    
    # Если меняется категория, проверяем её существование с проекцией
    if "category_id" in update_payload:
        category = await db.categories.find_one(
            {"_id": as_object_id(update_payload["category_id"])},
            {"_id": 1}  # Только ID для проверки существования
        )
        if not category:
            raise HTTPException(status_code=400, detail="Категория не найдена")
    
    # Конвертируем base64 изображения в GridFS file_id
    if "image" in update_payload and update_payload["image"]:
        if isinstance(update_payload["image"], str) and update_payload["image"].startswith("data:image"):
            # Это base64 изображение, сохраняем в GridFS
            # Сначала удаляем старое изображение если оно было
            old_product = await db.products.find_one({"_id": product_oid}, {"image": 1})
            if old_product and old_product.get("image"):
                # Удаляем старое изображение из GridFS (опционально, можно оставить для истории)
                pass
            
            image_file_id = await save_base64_image_to_gridfs(update_payload["image"])
            if image_file_id:
                update_payload["image"] = image_file_id
            else:
                # Если не удалось сохранить, удаляем поле из обновления
                update_payload.pop("image", None)
    
    if "images" in update_payload and update_payload["images"]:
        if isinstance(update_payload["images"], list) and len(update_payload["images"]) > 0:
            # Проверяем, есть ли base64 изображения в списке
            has_base64 = any(
                isinstance(img, str) and img.startswith("data:image")
                for img in update_payload["images"]
            )
            if has_base64:
                # Конвертируем base64 изображения
                image_ids = await save_base64_images_to_gridfs(update_payload["images"])
                if image_ids:
                    update_payload["images"] = image_ids
                else:
                    update_payload.pop("images", None)
    
    # Нормализуем изображения: синхронизируем image и images перед сохранением
    if "image" in update_payload or "images" in update_payload:
        # Получаем текущие данные товара для нормализации
        current_product = await db.products.find_one({"_id": product_oid}, {"image": 1, "images": 1})
        if current_product:
            # Объединяем текущие и новые данные для нормализации
            merged_data = {
                "image": update_payload.get("image", current_product.get("image")),
                "images": update_payload.get("images", current_product.get("images")),
            }
            normalized = normalize_product_images(merged_data)
            # Обновляем только измененные поля
            if "image" in normalized:
                update_payload["image"] = normalized["image"]
            if "images" in normalized:
                update_payload["images"] = normalized["images"]
        else:
            # Если товар не найден, нормализуем только новые данные
            normalized = normalize_product_images(update_payload)
            update_payload.update(normalized)
    
    # Обновляем с проекцией для минимизации данных
    doc = await db.products.find_one_and_update(
        {"_id": product_oid},
        {"$set": update_payload},
        return_document=True,
        projection={
            "name": 1,
            "description": 1,
            "price": 1,
            "image": 1,
            "images": 1,
            "category_id": 1,
            "available": 1,
            "variants": 1,
            "_id": 1,
        }
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Товар не найден")
    
    # Нормализуем изображения перед сериализацией
    normalized_doc = normalize_product_images(doc)
    serialized = serialize_doc(normalized_doc)
    serialized.pop("_id", None)  # Удаляем _id, так как используем id
    product = Product(**serialized | {"id": str(doc["_id"])})
    return product


@router.options("/product/image/{file_id}")
async def options_product_image(file_id: str):
    """Обрабатывает OPTIONS запрос для CORS preflight."""
    return Response(
        status_code=200,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Max-Age": "86400",  # 24 часа
        },
    )


@router.get("/product/image/{file_id}")
async def get_product_image(
    file_id: str,
):
    """Получает изображение продукта из GridFS по file_id."""
    try:
        fs = get_gridfs()
        loop = asyncio.get_event_loop()

        # Получаем файл из GridFS (синхронная операция в executor)
        grid_file = await loop.run_in_executor(None, lambda: fs.get(ObjectId(file_id)))
        file_data = await loop.run_in_executor(None, grid_file.read)
        filename = grid_file.filename or "product-image"
        content_type = grid_file.content_type or "image/jpeg"

        # Создаем Response с CORS заголовками
        response = Response(
            content=file_data,
            media_type=content_type,
            headers={
                "Content-Disposition": f'inline; filename="{filename}"',
                "Cache-Control": "public, max-age=31536000",  # Кешируем на 1 год
            },
        )
        
        # Явно добавляем CORS заголовки для изображений
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "*"
        response.headers["Access-Control-Expose-Headers"] = "*"
        
        return response
    except Exception as e:
        logger.error(f"Ошибка при загрузке изображения продукта {file_id}: {e}")
        raise HTTPException(status_code=404, detail=f"Изображение не найдено: {str(e)}")


@router.delete(
    "/admin/product/{product_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_product(
    product_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    _admin_id: int = Depends(verify_admin),
):
    """Удаляет товар и все его изображения из GridFS."""
    product_oid = as_object_id(product_id)
    
    # Получаем товар с изображениями перед удалением
    product_doc = await db.products.find_one(
        {"_id": product_oid},
        {"image": 1, "images": 1}
    )
    
    if not product_doc:
        raise HTTPException(status_code=404, detail="Товар не найден")
    
    # Удаляем изображения из GridFS
    await delete_product_images_from_gridfs(product_doc)
    
    # Удаляем товар из базы данных
    result = await db.products.delete_one({"_id": product_oid})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Товар не найден")
    
    return Response(status_code=status.HTTP_204_NO_CONTENT)
