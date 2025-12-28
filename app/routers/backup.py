"""Роутер для экспорта и импорта базы данных (бэкап)."""

import gzip
import io
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile, File, status
from fastapi.responses import StreamingResponse
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..auth import verify_backup_user
from ..database import get_db
from ..utils import serialize_doc

logger = logging.getLogger(__name__)

router = APIRouter(tags=["backup"])

# Список коллекций для экспорта/импорта
COLLECTIONS = [
    "categories",
    "products",
    "customers",
    "store_status",
    # carts и orders можно исключить или включить по необходимости
    # "carts",
    # "orders",
]


async def export_collection(db: AsyncIOMotorDatabase, collection_name: str) -> List[dict]:
    """
    Экспортирует коллекцию из базы данных.
    
    Args:
        db: Подключение к базе данных
        collection_name: Имя коллекции
        
    Returns:
        Список документов коллекции (сериализованных)
    """
    try:
        collection = db[collection_name]
        cursor = collection.find({})
        documents = await cursor.to_list(length=None)  # Получаем все документы
        
        # Сериализуем документы (преобразуем ObjectId в строки)
        serialized = []
        for doc in documents:
            serialized.append(serialize_doc(doc))
        
        logger.info(f"Экспортировано {len(serialized)} документов из коллекции {collection_name}")
        return serialized
    except Exception as e:
        logger.error(f"Ошибка при экспорте коллекции {collection_name}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Ошибка при экспорте коллекции {collection_name}: {str(e)}"
        )


async def import_collection(
    db: AsyncIOMotorDatabase,
    collection_name: str,
    documents: List[dict],
    clear_existing: bool = False
) -> int:
    """
    Импортирует коллекцию в базу данных.
    
    Args:
        db: Подключение к базе данных
        collection_name: Имя коллекции
        documents: Список документов для импорта
        clear_existing: Если True, очищает коллекцию перед импортом
        
    Returns:
        Количество импортированных документов
    """
    try:
        collection = db[collection_name]
        
        # Очищаем коллекцию, если нужно
        if clear_existing:
            await collection.delete_many({})
            logger.info(f"Коллекция {collection_name} очищена")
        
        if not documents:
            logger.info(f"Нет документов для импорта в коллекцию {collection_name}")
            return 0
        
        # Преобразуем строки обратно в ObjectId где необходимо
        processed_docs = []
        for doc in documents:
            processed_doc = _deserialize_doc(doc)
            processed_docs.append(processed_doc)
        
        # Вставляем документы
        if processed_docs:
            # Если clear_existing=False, используем upsert для обновления существующих
            if not clear_existing:
                # Используем replace_one с upsert для каждого документа
                count = 0
                for doc in processed_docs:
                    doc_id = doc.get("_id")
                    if doc_id:
                        await collection.replace_one({"_id": doc_id}, doc, upsert=True)
                        count += 1
                    else:
                        # Если нет _id, вставляем как новый документ
                        await collection.insert_one(doc)
                        count += 1
                logger.info(f"Импортировано/обновлено {count} документов в коллекцию {collection_name}")
                return count
            else:
                # Если clear_existing=True, просто вставляем все документы
                result = await collection.insert_many(processed_docs, ordered=False)
                count = len(result.inserted_ids)
                logger.info(f"Импортировано {count} документов в коллекцию {collection_name}")
                return count
        
        return 0
    except Exception as e:
        logger.error(f"Ошибка при импорте коллекции {collection_name}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Ошибка при импорте коллекции {collection_name}: {str(e)}"
        )


def _deserialize_doc(doc: dict) -> dict:
    """
    Десериализует документ, преобразуя строки ObjectId обратно в ObjectId.
    
    Обрабатывает поля _id и другие поля, которые могут содержать ObjectId
    (например, category_id, product_id и т.д.).
    
    Args:
        doc: Словарь для десериализации
        
    Returns:
        Десериализованный словарь
    """
    result = {}
    for key, value in doc.items():
        # Преобразуем _id в ObjectId
        if key == "_id" and isinstance(value, str):
            try:
                if ObjectId.is_valid(value):
                    result[key] = ObjectId(value)
                else:
                    result[key] = value
            except Exception:
                result[key] = value
        # Преобразуем поля, заканчивающиеся на _id, в ObjectId (если это валидный ObjectId)
        elif key.endswith("_id") and isinstance(value, str):
            try:
                if ObjectId.is_valid(value):
                    result[key] = ObjectId(value)
                else:
                    result[key] = value
            except Exception:
                result[key] = value
        elif isinstance(value, dict):
            result[key] = _deserialize_doc(value)
        elif isinstance(value, list):
            result[key] = [
                _deserialize_doc(item) if isinstance(item, dict) else (
                    ObjectId(item) if isinstance(item, str) and ObjectId.is_valid(item) else item
                )
                for item in value
            ]
        else:
            result[key] = value
    return result


@router.get("/admin/backup/export")
async def export_database(
    collections: Optional[str] = None,
    include_carts: bool = False,
    include_orders: bool = False,
    db: AsyncIOMotorDatabase = Depends(get_db),
    _backup_user_id: int = Depends(verify_backup_user),
):
    """
    Экспортирует базу данных в JSON файл.
    
    Args:
        collections: Список коллекций через запятую (если не указан, используются стандартные)
        include_carts: Включить корзины в экспорт
        include_orders: Включить заказы в экспорт
        db: Подключение к базе данных
        _backup_user_id: ID пользователя с правами бэкапа (проверка прав)
        
    Returns:
        JSON файл с данными базы данных
    """
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="База данных недоступна"
        )
    
    # Определяем список коллекций для экспорта
    collections_to_export = COLLECTIONS.copy()
    
    if include_carts:
        collections_to_export.append("carts")
    if include_orders:
        collections_to_export.append("orders")
    
    # Если указаны конкретные коллекции, используем их
    if collections:
        collections_to_export = [c.strip() for c in collections.split(",") if c.strip()]
    
    # Экспортируем все коллекции
    backup_data = {
        "version": "1.0",
        "exported_at": datetime.utcnow().isoformat(),
        "collections": {}
    }
    
    for collection_name in collections_to_export:
        try:
            documents = await export_collection(db, collection_name)
            backup_data["collections"][collection_name] = documents
        except Exception as e:
            logger.error(f"Ошибка при экспорте коллекции {collection_name}: {e}")
            # Продолжаем экспорт других коллекций даже при ошибке
            backup_data["collections"][collection_name] = []
    
    # Создаем JSON строку
    json_str = json.dumps(backup_data, ensure_ascii=False, indent=2, default=str)
    json_bytes = json_str.encode("utf-8")
    
    # Сжимаем данные
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb") as gzip_file:
        gzip_file.write(json_bytes)
    compressed_data = buffer.getvalue()
    
    # Создаем имя файла с датой
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"backup_{timestamp}.json.gz"
    
    # Возвращаем файл
    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/gzip",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "Content-Type": "application/gzip",
        }
    )


@router.post("/admin/backup/import")
async def import_database(
    file: UploadFile = File(...),
    clear_existing: bool = False,
    db: AsyncIOMotorDatabase = Depends(get_db),
    _backup_user_id: int = Depends(verify_backup_user),
):
    """
    Импортирует базу данных из JSON файла.
    
    Args:
        file: JSON файл с данными базы данных
        clear_existing: Если True, очищает существующие коллекции перед импортом
        db: Подключение к базе данных
        _backup_user_id: ID пользователя с правами бэкапа (проверка прав)
        
    Returns:
        Результат импорта
    """
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="База данных недоступна"
        )
    
    # Проверяем тип файла
    if not file.filename.endswith((".json", ".json.gz", ".gz")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Файл должен быть в формате JSON или JSON.GZ"
        )
    
    try:
        # Читаем файл
        file_content = await file.read()
        
        # Распаковываем, если это gzip
        if file.filename.endswith((".gz", ".json.gz")):
            buffer = io.BytesIO(file_content)
            with gzip.GzipFile(fileobj=buffer, mode="rb") as gzip_file:
                json_bytes = gzip_file.read()
        else:
            json_bytes = file_content
        
        # Парсим JSON
        try:
            backup_data = json.loads(json_bytes.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Ошибка при парсинге JSON: {str(e)}"
            )
        
        # Проверяем структуру данных
        if "collections" not in backup_data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Неверный формат файла бэкапа: отсутствует поле 'collections'"
            )
        
        # Импортируем коллекции
        import_results = {}
        total_imported = 0
        
        for collection_name, documents in backup_data["collections"].items():
            try:
                count = await import_collection(db, collection_name, documents, clear_existing)
                import_results[collection_name] = {
                    "status": "success",
                    "imported": count
                }
                total_imported += count
            except Exception as e:
                logger.error(f"Ошибка при импорте коллекции {collection_name}: {e}")
                import_results[collection_name] = {
                    "status": "error",
                    "error": str(e)
                }
        
        return {
            "status": "success",
            "total_imported": total_imported,
            "collections": import_results,
            "exported_at": backup_data.get("exported_at"),
            "version": backup_data.get("version")
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ошибка при импорте базы данных: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Ошибка при импорте базы данных: {str(e)}"
        )


@router.get("/admin/backup/info")
async def backup_info(
    db: AsyncIOMotorDatabase = Depends(get_db),
    _backup_user_id: int = Depends(verify_backup_user),
):
    """
    Получает информацию о коллекциях базы данных.
    
    Args:
        db: Подключение к базе данных
        _backup_user_id: ID пользователя с правами бэкапа (проверка прав)
        
    Returns:
        Информация о коллекциях (название и количество документов)
    """
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="База данных недоступна"
        )
    
    try:
        # Получаем список всех коллекций
        collection_names = await db.list_collection_names()
        
        # Получаем количество документов в каждой коллекции
        collections_info = {}
        for collection_name in collection_names:
            try:
                count = await db[collection_name].count_documents({})
                collections_info[collection_name] = count
            except Exception as e:
                logger.error(f"Ошибка при получении информации о коллекции {collection_name}: {e}")
                collections_info[collection_name] = "error"
        
        return {
            "collections": collections_info,
            "total_collections": len(collection_names)
        }
    except Exception as e:
        logger.error(f"Ошибка при получении информации о базе данных: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Ошибка при получении информации: {str(e)}"
        )

