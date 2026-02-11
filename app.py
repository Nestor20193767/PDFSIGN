from flask import Flask, render_template, request, jsonify, send_file
import base64
import os
import uuid
import json
import io
import fitz
from datetime import datetime
from PIL import Image

app = Flask(__name__)

UPLOAD_FOLDER = 'documentos_firmados'
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

DELIMITER = "$$$"


# --- LÓGICA DE ESTEGANOGRAFÍA ---
def ocultar_metadatos(img_pil, mensaje_json):
    data = mensaje_json + DELIMITER
    binary_data = ''.join(format(ord(i), '08b') for i in data)

    # IMPORTANTE: Usamos RGB en lugar de RGBA para evitar problemas en el PDF
    img = img_pil.convert('RGB')
    pixels = img.load()
    width, height = img.size
    data_index = 0
    data_len = len(binary_data)

    for y in range(height):
        for x in range(width):
            if data_index < data_len:
                r, g, b = pixels[x, y]
                # Modificar bit LSB del canal Rojo
                r = (r & ~1) | int(binary_data[data_index])
                pixels[x, y] = (r, g, b)
                data_index += 1
            else:
                return img
    return img


def extraer_metadatos_de_imagen(img_pil):
    # Convertimos a RGB para lectura estándar
    img = img_pil.convert('RGB')
    pixels = img.load()
    width, height = img.size
    binary_data = ""

    for y in range(height):
        for x in range(width):
            r, g, b = pixels[x, y]
            binary_data += str(r & 1)

    decoded_text = ""
    for i in range(0, len(binary_data), 8):
        byte = binary_data[i:i + 8]
        if len(byte) < 8: break
        try:
            char = chr(int(byte, 2))
            decoded_text += char
            if DELIMITER in decoded_text:
                return decoded_text.replace(DELIMITER, "")
        except:
            continue
    return None


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/procesar_firma', methods=['POST'])
def procesar_firma():
    try:
        if 'pdf_file' not in request.files:
            return jsonify({'status': 'error', 'msg': 'Falta el archivo PDF'}), 400

        pdf_file = request.files['pdf_file']
        firma_base64 = request.form['firma_base64']
        email_usuario = request.form['email']
        device_data = request.form['device_data']

        # 1. Procesar la firma (Canvas viene transparente)
        image_data = firma_base64.split(",")[1]
        img_bytes = base64.b64decode(image_data)
        img_original = Image.open(io.BytesIO(img_bytes)).convert('RGBA')

        # --- SOLUCIÓN CRÍTICA: APLANAR TRANSPARENCIA ---
        # Creamos un fondo blanco
        fondo_blanco = Image.new("RGB", img_original.size, (255, 255, 255))
        # Pegamos la firma transparente encima (usando el canal alfa como máscara)
        fondo_blanco.paste(img_original, mask=img_original.split()[3])

        # Redimensionar si es muy grande (opcional, ayuda a que no se comprima)
        # fondo_blanco.thumbnail((300, 150))

        # 2. Preparar Metadatos
        token_unico = str(uuid.uuid4())
        datos_dict = {
            "token": token_unico,
            "email": email_usuario,
            "fecha": datetime.now().isoformat(),
            "dispositivo": json.loads(device_data)
        }
        json_string = json.dumps(datos_dict)

        # 3. Inyectar Esteganografía en la imagen PLANA (RGB)
        img_con_datos = ocultar_metadatos(fondo_blanco, json_string)

        img_byte_arr = io.BytesIO()
        # Guardamos como PNG con máxima calidad
        img_con_datos.save(img_byte_arr, format='PNG', compress_level=0)
        img_final_bytes = img_byte_arr.getvalue()

        # 4. Insertar en el PDF
        doc = fitz.open(stream=pdf_file.read(), filetype="pdf")
        page = doc[-1]

        # Ubicación: Esquina inferior derecha
        rect = fitz.Rect(page.rect.width - 250, page.rect.height - 150, page.rect.width - 50, page.rect.height - 50)

        # Insertamos la imagen. PyMuPDF respetará los bits al ser RGB opaco.
        page.insert_image(rect, stream=img_final_bytes)

        # --- DOBLE FACTOR DE SEGURIDAD ---
        # Guardamos TAMBIÉN los metadatos en el propio archivo PDF (invisible al usuario)
        # Esto garantiza que si la imagen falla, el PDF sigue siendo validable.
        doc.set_metadata({
            **doc.metadata,
            "keywords": json_string,  # Usamos el campo keywords para guardar el JSON
            "subject": f"Firmado digitalmente por {email_usuario}"
        })

        output_filename = f"firmado_{token_unico[:8]}.pdf"
        output_path = os.path.join(UPLOAD_FOLDER, output_filename)

        # Guardamos sin compresión extra
        doc.save(output_path, garbage=0, deflate=False)

        return jsonify({
            'status': 'success',
            'download_url': f"/descargar/{output_filename}",
            'token': token_unico
        })

    except Exception as e:
        print(f"Error: {e}")
        return jsonify({'status': 'error', 'msg': str(e)}), 500


@app.route('/descargar/<filename>')
def descargar_archivo(filename):
    return send_file(os.path.join(UPLOAD_FOLDER, filename), as_attachment=True)


@app.route('/validar_pdf', methods=['POST'])
def validar_pdf():
    try:
        file = request.files['file']
        doc = fitz.open(stream=file.read(), filetype="pdf")
        firmas_encontradas = []

        # MÉTODO A: Buscar en Metadatos del PDF (Más robusto)
        try:
            meta_keywords = doc.metadata.get('keywords', '')
            if meta_keywords and 'token' in meta_keywords and 'email' in meta_keywords:
                datos_meta = json.loads(meta_keywords)
                datos_meta['origen'] = 'METADATA_PDF'  # Etiqueta para saber de dónde vino
                firmas_encontradas.append(datos_meta)
        except:
            pass

        # MÉTODO B: Buscar Esteganografía en Imágenes (Tu requerimiento)
        for page_num in range(len(doc)):
            for img in doc.get_page_images(page_num):
                xref = img[0]
                base_image = doc.extract_image(xref)

                # Solo analizamos PNGs (evitar JPEGs comprimidos)
                if base_image['ext'] != 'png':
                    continue

                try:
                    img_pil = Image.open(io.BytesIO(base_image["image"]))
                    datos_ocultos = extraer_metadatos_de_imagen(img_pil)
                    if datos_ocultos:
                        json_data = json.loads(datos_ocultos)
                        json_data['origen'] = 'ESTEGANOGRAFIA_IMAGEN'
                        firmas_encontradas.append(json_data)
                except:
                    continue

        if firmas_encontradas:
            # Eliminar duplicados si ambos métodos encuentran lo mismo
            unique_firmas = {v['token']: v for v in firmas_encontradas}.values()
            return jsonify({'status': 'success', 'cantidad': len(unique_firmas), 'datos': list(unique_firmas)})
        else:
            return jsonify({'status': 'error', 'msg': 'No se detectó firma válida.'}), 404

    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True, port=7000)
