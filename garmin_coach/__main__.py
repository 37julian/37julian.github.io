"""python -m garmin_coach → startet den Webserver."""
import uvicorn
from .config import PORT

uvicorn.run("garmin_coach.app:app", host="127.0.0.1", port=PORT, reload=True)
