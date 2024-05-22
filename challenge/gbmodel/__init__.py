import os

model_backend = os.getenv('GUESTBOOK_SERVICE', 'sqlite3')
# model_backend = 'pylist'
# model_backend = 'postgres'
# model_backend = 'sqlite3'

if model_backend == 'sqlite3':
    from .model_sqlite3 import ModelSqlite as model
elif model_backend == 'pylist':
    from .model_pylist import ModelPylist as model
elif model_backend == 'postgres':
    from .model_sql_postgres import ModelSqlPostgres as model
else:
    raise ValueError("No appropriate databackend configured. ")

appmodel = model()

def get_model():
    return appmodel
