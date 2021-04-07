import io
import cbor2
import segno as qr
import ujson
import zlib

from base45 import b45encode
from cose.algorithms import Es256
from cose.curves import P256
from cose.headers import Algorithm, KID
from cose.keys import CoseKey
from cose.keys.keyparam import KpAlg, EC2KpD, EC2KpCurve
from cose.keys.keyparam import KpKty
from cose.keys.keytype import KtyEC2
from cose.messages import Sign1Message
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from flask import (
    Flask,
    request,
    send_from_directory,
    Response,
    render_template,
    send_file,
)
from werkzeug.routing import BaseConverter

from disclosure_level import DisclosureLevel
from fhir_query import FhirQuery
from min_data_set import MinDataSetFactory, MinDataSet

app = Flask(__name__)

_page_state: dict = {}


class RegexConverter(BaseConverter):
    def __init__(self, url_map, *items):
        super(RegexConverter, self).__init__(url_map)
        self.regex = items[0]


app.url_map.converters["regex"] = RegexConverter


@app.route("/")
def index():
    return render_template("index.html")


@app.route('/<regex("scripts.js|styles.css|fhir_query_res.json"):file>')
def scripts_styles(file):
    return send_from_directory(directory="static", filename=file)


@app.route('/<regex("[a-z0-9\-]+\.(pem|key)"):file>')
def cryptofile(file):
    return send_from_directory(
        directory="static", filename=file, mimetype="application/x-pem-file"
    )


@app.route("/query_fhir_server", methods=["POST", "GET"])
def query_fhir_server():
    fhir_server = request.form["fhir_server"] if "fhir_server" in request.form else None
    _page_state["qry_res"] = FhirQuery(fhir_server=fhir_server).find()
    return render_template("index.html", page_state=_page_state)


@app.route("/fhir2json", methods=["POST", "GET"])
def fhir2json():
    # pre-cond: /query_fhir_server called prior in order to set: _page_state["qry_res"]
    min_data_set: MinDataSet = MinDataSetFactory.create(DisclosureLevel.PV)
    min_data_set.parse(qry_res=_page_state["qry_res"])
    _page_state["min_data_set"] = min_data_set.as_json()
    return render_template("index.html", page_state=_page_state)


@app.route("/fhir2jsonld", methods=["POST", "GET"])
def fhir2jsonld():
    # pre-cond: /query_fhir_server called prior in order to set: _page_state["qry_res"]
    min_data_set: MinDataSet = MinDataSetFactory.create(DisclosureLevel.PV)
    min_data_set.parse(qry_res=_page_state["qry_res"])
    _page_state["min_data_set_jsonld"] = min_data_set.as_jsonld()
    return render_template("index.html", page_state=_page_state)


@app.route("/fhir2jsoncbor", methods=["POST", "GET"])
def fhir2jsoncbor():
    cb = cbor2.dumps(_page_state["min_data_set_jsonld"])
    _page_state["min_data_set_jsonld_cborld"] = cb
    return render_template("index.html", page_state=_page_state)


@app.route("/fhir2jsoncborcose", methods=["POST", "GET"])
def fhir2jsoncborcose():
    with open("dsc-worker.pem", "rb") as file:
        pem = file.read()
    cert = x509.load_pem_x509_certificate(pem)
    fingerprint = cert.fingerprint(hashes.SHA256())
    keyid = fingerprint[-8:]

    with open("dsc-worker.key", "rb") as file:
        pem = file.read()
    keyfile = load_pem_private_key(pem, password=None)
    priv = keyfile.private_numbers().private_value.to_bytes(32, byteorder="big")

    data = _page_state["min_data_set_jsonld_cborld"]
    msg = Sign1Message(phdr={Algorithm: Es256, KID: keyid}, payload=data)

    cose_key = {
        KpKty: KtyEC2,
        KpAlg: Es256,  # ecdsa-with-SHA256
        EC2KpCurve: P256,  # Ought to be pk.curve - but the two libs clash
        EC2KpD: priv,
    }
    msg.key = CoseKey.from_dict(cose_key)

    _page_state["min_data_set_jsoncborcose"] = msg.encode()
    return render_template("index.html", page_state=_page_state)


@app.route("/fhir2jsoncborcosezlib", methods=["POST", "GET"])
def fhir2jsoncborcosezlib():
    data = _page_state["min_data_set_jsoncborcose"]
    _page_state["cose_compress"] = zlib.compress(data, 9)
    return render_template("index.html", page_state=_page_state)


@app.route("/fhir2jsoncborcosezlibb45", methods=["POST", "GET"])
def fhir2jsoncborcosezlibb45():
    _page_state["b45"] = b45encode(_page_state["cose_compress"])
    return render_template("index.html", page_state=_page_state)


@app.route("/fhir2b45qr", methods=["POST", "GET"])
def fhir2jsoncborcosezlibb45qr():
    data = _page_state["b45"]
    # return qr.make(data,error='Q').svg_inline()
    buff = io.BytesIO()
    qr.make(data, error="Q").save(buff, kind="png", scale=6)
    buff.seek(0)
    return send_file(buff, mimetype="image/png")


@app.route("/fhir2size", methods=["POST", "GET"])
def fhir2size():
    step1 = fhir2json()

    len_fhir = 0
    len_json = len(ujson.dumps(fhir2json()))
    len_cbor = len(fhir2jsoncbor())
    len_cose = len(fhir2jsoncborcose())
    len_zlib = len(fhir2jsoncborcosezlib())

    b45 = fhir2jsoncborcosezlibb45()
    len_b45 = len(b45)

    img = qr.make(b45, error="Q")
    img_s = len(img.matrix)

    win = len_json - len_zlib
    winp = int(100 * win / len_json)

    return Response(
        f"Sizes:\n JSON:   {len_json}\n CBOR:   {len_cbor}\n COSE:   {len_cose}\n ZLIB:   {len_zlib}\n B45 :   {len_b45}\n QR mode: {img.mode} (Should be 2/alphanumeric/b45)\n QR code: {img.version} (1..40)\n QR matrix: {img_s}x{img_s} (raw pixels without border)\n\nWin: {winp}%; {win} bytes saved on {len_json} bytes (FHIR was {len_fhir} bytes)",
        mimetype="text/plain",
    )
