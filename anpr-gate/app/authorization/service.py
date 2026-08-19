from .database import PlateDatabase


class AuthorizationService:
    def __init__(self, database: PlateDatabase) -> None:
        self.database = database

    def is_allowed(self, plate: str) -> bool:
        return self.database.contains(plate)
