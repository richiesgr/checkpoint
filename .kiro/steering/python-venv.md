---
inclusion: always
---
# Python environment (venv)

- Use a project-local virtual environment at `.venv/` in the workspace root. Never install packages into the global or conda `base` environment.
- Required versions: **Python 3.13.9** and **Apache Spark / PySpark 4.3.x** (Spark 4.3 supports Python 3.11+, so 3.13 is supported, and Java 17, 21 or 25). Pin the exact PySpark patch release in `requirements.txt` (`pyspark==4.3.<patch>`). Record the exact Python, PySpark and JDK versions in the README.
- Create the venv from the conda base interpreter, called by its full path, and never use `conda` itself (no `conda activate`, `conda install` or `conda create`): `& 'C:\Users\Richard\miniconda3\python.exe' -m venv .venv`. Check that `.\.venv\Scripts\python.exe --version` prints 3.13.9. Do not build the venv from any other interpreter. Once the venv exists, use only the venv interpreter.
- The Spark Kafka connector must match the Spark and Scala build: `org.apache.spark:spark-sql-kafka-0-10_2.13:<same version as pyspark>`. Set it with `spark.jars.packages` in the session builder, or ship the jar. The Kafka-marked tests need it, and the README must say how it is resolved.
- Activate on Windows PowerShell: `.\.venv\Scripts\Activate.ps1`. On Linux or macOS: `source .venv/bin/activate`. In scripted commands, call the interpreter directly (`.\.venv\Scripts\python.exe -m pytest`) so no activation is needed.
- Always install with `python -m pip`, from a pinned `requirements.txt` (exact versions, for example `pyspark==<x.y.z>`). If dev tools are separate, use `requirements-dev.txt`. Do not leave unpinned dependencies.
- PySpark 4.3 needs JDK 17, 21 or 25 (JDK 21 is installed on this machine). Record the JDK version in the README and check `java -version` in setup instructions. On Windows, Spark may also need `HADOOP_HOME` with `winutils`. Document it if required.
- Make sure Spark uses the venv interpreter: set `PYSPARK_PYTHON` and `PYSPARK_DRIVER_PYTHON` to the venv's `python` in the Spark session fixture and in the CLIs.
- Run every project command (tests, CLIs, linters) through the venv. The README's commands must work from a fresh clone: create venv, install requirements, run tests.
- `.venv/` is never committed. `requirements.txt` is.