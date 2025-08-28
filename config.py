import os
from dotenv import load_dotenv

# загружаем переменные окружения
load_dotenv()

class Config: 
        # токен
        vk_token = os.getenv('vk_token')

        # наши ID
        developer_id = os.getenv('developer_id')

        # настройка логов
        log_level = os.getenv('log_level', 'info')

        # id group
        group_id = os.getenv('group_id', None)

        # long pool
        wait_timeout = int(os.getenv('wait_timeout', '25'))

        # проверка параметров
        @classmethod 
        def valide(csl):
                """Проверка обязательных параметров конфигурации"""
                if not csl.vk_token == 'your_group_token_here':
                        raise ValueError("Укажи токен vk_token в .env")

                return True
