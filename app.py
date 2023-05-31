import uvicorn
from fastapi import FastAPI, Request
from PlaneWave import pwi4_client
from unit import Unit
from utils import init_log, PrettyJSONResponse, HelpResponse, quote, Subsystem
import inspect
from mastapi import Mastapi
from openapi import make_openapi_schema

import logging

unit_id = 17
logger = logging.getLogger('mast')
init_log(logger)

app = FastAPI(docs_url='/docs', redocs_url=None, openapi_url='/mast/api/v1/openapi.json')
pw = pwi4_client.PWI4()
unit = Unit(unit_id)
root = '/mast/api/v1/'


subsystems = [
    Subsystem(path='unit', obj=unit, obj_name='unit'),
    Subsystem(path='mount', obj=unit.mount, obj_name='unit.mount'),
    Subsystem(path='focuser', obj=unit.focuser, obj_name='unit.focuser'),
    Subsystem(path='camera', obj=unit.camera, obj_name='unit.camera'),
    Subsystem(path='stage', obj=unit.stage, obj_name='unit.stage'),
    Subsystem(path='covers', obj=unit.covers, obj_name='unit.covers'),
    Subsystem(path='planewave', obj=pw, obj_name='pw')
]

make_openapi_schema(app=app, subsystems=subsystems)


@app.get(root + '{subsystem}/{method}', response_class=PrettyJSONResponse)
def do_item(subsystem: str, method: str, request: Request):

    sub = [s for s in subsystems if s.path == subsystem]
    if len(sub) == 0:
        return f'Invalid MAST subsystem \"{subsystem}\", valid ones: {", ".join([x.path for x in subsystems])}'

    sub = sub[0]
    # api_methods = list()
    api_method_names = list()
    # inspect.getmembers returns (name, object) all_method_tuples
    all_method_tuples = inspect.getmembers(sub.obj, inspect.ismethod)
    api_method_tuples = [t for t in all_method_tuples if Mastapi.is_api_method(t[1])]
    # for tup in all_method_tuples:
    #     if Mastapi.is_api_method(tup[1]):
    #         api_method_names.append(tup[0])
    #         api_methods.append(tup[1])
    api_method_names = [t[0] for t in api_method_tuples]
    api_method_objects = [t[1] for t in api_method_tuples]

    if method == 'help':
        responses = list()
        for i, obj in enumerate(api_method_objects):
            responses.append(HelpResponse(api_method_names[i],
                                          api_method_objects[i].__doc__.replace(':mastapi:\n', '').lstrip('\n').strip()))
        return responses

    if method not in api_method_names:
        return f'Invalid method "{method}" for subsystem {subsystem}, valid ones: {", ".join(api_method_names)}'

    cmd = f'{sub.obj_name}.{method}('
    for k, v in request.query_params.items():
        cmd += f"{k}={quote(v)}, "
    cmd = cmd.removesuffix(', ') + ')'
    # try:
    return eval(cmd)
    # except Exception as ex:
    #     ret = ResultWithStatus()
    #     ret.status = None
    #     ret.error = ex
    #     ret.result = None
    #     return ret


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
