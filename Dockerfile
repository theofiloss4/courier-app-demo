# =============================================================================
# Builds the container image for the FastAPI application service (referenced
# by `build: .` in docker-compose.yml). This is a single-stage build: the
# image contains Python, the installed dependencies, and the application
# source code, and simply runs Uvicorn as its startup command.
# =============================================================================
# "slim" is a minimal Debian-based Python image - much smaller than the
# default python:3.12 image, since it omits build tools/docs not needed at
# runtime (dependencies here are pure-Python or ship prebuilt wheels).
FROM python:3.12-slim

# PYTHONDONTWRITEBYTECODE stops Python from writing .pyc cache files into
# the image (unnecessary in a container that is rebuilt from scratch each
# time, not reused across runs). PYTHONUNBUFFERED makes print()/log output
# appear immediately in `docker compose logs` instead of being buffered.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# All application files are installed and executed from this container directory.
WORKDIR /app

# Copying just the dependency manifest first (before the app source code)
# lets Docker cache the `pip install` layer below - as long as
# pyproject.toml does not change, a rebuild after only editing app/ code
# can reuse this cached layer instead of reinstalling every dependency.
COPY pyproject.toml README.md ./
COPY app ./app

# Install the package declared in pyproject.toml without retaining pip cache files.
RUN pip install --no-cache-dir .

# Documents which port the application listens on; does not itself publish
# the port to the host - that is docker-compose.yml's `ports:` mapping.
EXPOSE 8001

# Start Uvicorn with the FastAPI instance defined in app/main.py.
# --host 0.0.0.0 is required (not the default 127.0.0.1) so the server
# accepts connections from OUTSIDE the container, not just from within it.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
