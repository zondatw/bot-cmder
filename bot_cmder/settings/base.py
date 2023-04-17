from environs import Env

env = Env()
env.read_env()

TELEGRAM_TOKEN = env("TELEGRAM_TOKEN")
