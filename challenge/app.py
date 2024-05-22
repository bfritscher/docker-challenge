from flask import abort, request, send_from_directory
import flask
import gbmodel

app = flask.Flask(__name__, static_url_path='') 

@app.route('/')
def serve_index():
    return send_from_directory('static', 'index.html')

@app.route('/entries')
def entries():
    model = gbmodel.get_model()
    if request.method == 'GET':
        entries = [dict(name=row[0], email=row[1], signed_on=row[2].isoformat(), message=row[3] ) for row in model.select()]
        return entries
    
    return abort(403)

@app.route('/entry', methods=['POST'])
def entry():
    """ Guestbook API endpoint
        :param request: flask.Request object
        :return: flack.Response object (in JSON), HTTP status code
    """
    model = gbmodel.get_model()

    if request.method == 'POST' and request.headers['content-type'] == 'application/json':
        request_json = request.get_json(silent=True)

        if all(key in request_json for key in ('name', 'email', 'message')):
            model.insert(request_json['name'], request_json['email'], request_json['message'])
        else:
            raise ValueError("JSON missing name, email, or message property")
        return '', 204

    return abort(403)

if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True)
