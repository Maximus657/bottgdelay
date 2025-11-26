import aiohttp
import logging

logger = logging.getLogger(__name__)

class AsyncYandexDisk:
    """
    Класс для асинхронной работы с Яндекс.Диском.
    """
    def __init__(self, token, folder_name):
        self.token = token
        self.headers = {"Authorization": f"OAuth {token}"}
        self.folder_name = folder_name
        self.api_url = "https://cloud-api.yandex.net/v1/disk/resources"

    async def _ensure_folder(self, session):
        """Проверяет наличие папки и создает её при необходимости."""
        url = f"{self.api_url}?path={self.folder_name}"
        async with session.put(url, headers=self.headers) as resp:
            pass # Игнорируем ошибку, если папка уже есть

    async def upload_file(self, file_bytes, file_name):
        """
        Асинхронная загрузка файла.
        :param file_bytes: байты файла или поток (BytesIO)
        :param file_name: имя файла для сохранения
        :return: публичная ссылка на файл или None
        """
        async with aiohttp.ClientSession() as session:
            try:
                # 1. Убедимся, что папка существует
                await self._ensure_folder(session)

                full_path = f"{self.folder_name}/{file_name}"
                
                # 2. Получаем ссылку для загрузки (GET request)
                upload_req_url = f"{self.api_url}/upload"
                params = {"path": full_path, "overwrite": "true"}
                
                async with session.get(upload_req_url, headers=self.headers, params=params) as resp:
                    if resp.status != 200:
                        logger.error(f"YD Get Link Error: {await resp.text()}")
                        return None
                    data = await resp.json()
                    upload_link = data.get('href')

                # 3. Загружаем сам файл (PUT request)
                async with session.put(upload_link, data=file_bytes) as upload_resp:
                    if upload_resp.status != 201:
                        logger.error(f"YD Upload Error: {upload_resp.status}")
                        return None

                # 4. Публикуем (делаем файл доступным)
                publish_url = f"{self.api_url}/publish"
                async with session.put(publish_url, headers=self.headers, params={"path": full_path}) as pub_resp:
                    pass 

                # 5. Получаем публичную ссылку
                async with session.get(self.api_url, headers=self.headers, params={"path": full_path}) as meta_resp:
                    if meta_resp.status == 200:
                        meta = await meta_resp.json()
                        return meta.get('public_url')
                    return None
            except Exception as e:
                logger.error(f"YD Exception: {e}")
                return None
