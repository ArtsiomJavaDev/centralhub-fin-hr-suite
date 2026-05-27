from dataclasses import dataclass


@dataclass
class DbConfig:
    driver: str
    server: str
    database: str
    username: str = ""
    password: str = ""
    trusted_connection: bool = True
