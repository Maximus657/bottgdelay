from aiogram import Router

from .common import router as common_router
from .users import router as users_router
from .releases import router as releases_router
from .tasks import router as tasks_router
from .reports import router as reports_router
from .artists import router as artists_router

router = Router()

router.include_router(common_router)
router.include_router(users_router)
router.include_router(releases_router)
router.include_router(tasks_router)
router.include_router(reports_router)
router.include_router(artists_router)
