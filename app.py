import os
import psycopg2
from flask import Flask, jsonify, render_template, request, redirect, url_for, session, flash
from dotenv import load_dotenv
from werkzeug.utils import secure_filename

# Cargar las credenciales desde el archivo .env
load_dotenv()

app = Flask(__name__)

# Configuración para subida de imágenes
app.config['UPLOAD_FOLDER'] = os.path.join('static', 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # Límite máximo de 5 mb por foto
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# VITAL PARA QUE LA SESIÓN FUNCIONE
app.secret_key = os.getenv('SECRET_KEY', 'llave_super_secreta_de_desarrollo')

# Función global para establecer la conexión con PostgreSQL
def get_db_connection():
    conn = psycopg2.connect(
        host=os.getenv('DB_HOST', 'localhost'),
        port=os.getenv('DB_PORT', '5432'),
        database=os.getenv('DB_NAME', 'incidencias_va_db'),
        user=os.getenv('DB_USER', 'postgres'),
        password=os.getenv('DB_PASSWORD')
    )
    return conn

# 1. Ruta de la Pantalla Principal 
@app.route('/')
def index():
    return render_template('index.html')

# El Panel de Control Privado
@app.route('/dashboard')
def dashboard():
    # Protegemos la ruta: si no hay sesión, lo pateamos al login
    if not session.get('usuario_id'):
        flash('Acceso denegado. Por favor, inicie sesión.', 'danger')
        return redirect(url_for('login'))
        
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # 1. Contar Reclamos Activos 
        cur.execute("""
            SELECT COUNT(*) FROM tickets 
            WHERE estado_id NOT IN (
                SELECT id FROM estados_ticket WHERE nombre IN ('Resuelto', 'Desestimado')
            );
        """)
        activos = cur.fetchone()[0]
        
        # 2. Contar operativos (Sumamos los que están en 'En Proceso' y los 'Asignados')
        cur.execute("""
            SELECT COUNT(*) FROM tickets 
            WHERE estado_id IN (
                SELECT id FROM estados_ticket WHERE nombre IN ('En Proceso', 'Asignado')
            );
        """)
        en_proceso = cur.fetchone()[0]
        
        # 3. tickets Resueltos
        cur.execute("""
            SELECT COUNT(*) FROM tickets 
            WHERE estado_id = (SELECT id FROM estados_ticket WHERE nombre = 'Resuelto');
        """)
        resueltos = cur.fetchone()[0]
        
        cur.close()
        conn.close()
        
        # Enviamos las variables reales directamente a la plantilla dashboard.html
        return render_template('dashboard.html', activos=activos, en_proceso=en_proceso, resueltos=resueltos)
        
    except Exception as e:
        flash(f'Error al cargar métricas del panel: {str(e)}', 'danger')
        return render_template('dashboard.html', activos=0, en_proceso=0, resueltos=0)

# 2. Ruta para validar la conexión a la base de datos
@app.route('/test-db')
def test_db():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Hacemos una consulta rápida para verificar que el trigger y los datos iniciales respondan
        cur.execute('SELECT count(*) FROM estados_ticket;')
        cantidad_estados = cur.fetchone()[0]
        
        cur.close()
        conn.close()
        
        return jsonify({
            "estado": "Conexión a PostgreSQL EXITOSA",
            "estados_iniciales_detectados": cantidad_estados
        })
    except Exception as e:
        return jsonify({
            "estado": "ERROR de conexión",
            "detalle_error": str(e)
        }), 500

# 3. Ruta para el formulario de Nuevo Reclamo
@app.route('/nuevo-reclamo')
def nuevo_reclamo():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Buscamos todas las tipologías activas en la base de datos
        cur.execute("SELECT id, nombre FROM tipologias WHERE activo = TRUE ORDER BY nombre;")
        tipologias = cur.fetchall()
        
        cur.close()
        conn.close()
        
        # Le pasamos la variable 'tipologias' al HTML
        return render_template('nuevo_reclamo.html', tipologias=tipologias)
    except Exception as e:
        return f"Error al cargar el formulario: {str(e)}"

# 4. Ruta para el Inicio de Sesión
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        usuario_ingresado = request.form['usuario']
        password_ingresada = request.form['password']
        
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            
            cur.execute("""
                SELECT id, nombre_completo, rol, password_hash 
                FROM usuarios 
                WHERE (correo = %s OR dni = %s) AND activo = TRUE;
            """, (usuario_ingresado, usuario_ingresado))
            
            user = cur.fetchone()
            cur.close()
            conn.close()
            
            if user and user[3] == password_ingresada:
               
                session['usuario_id'] = user[0]
                session['nombre'] = user[1]
                session['rol'] = user[2]
            
            
                # LÓGICA DE ROLES (Basado en RF01)
                
                if session['rol'] == 'Administrador':
                    # El Administrador va directo a su Panel de Control (Dashboard)
                    return redirect(url_for('dashboard'))
                elif session['rol'] == 'Ciudadano':
                    # El Ciudadano va directo a reportar su incidencia (Mapa)
                    return redirect(url_for('nuevo_reclamo'))
                elif session['rol'] == 'Operador':
                    # El Operador irá a su lista de tareas por ahora lo mandamos al inicio
                    return redirect(url_for('index'))
                
            else:
                flash('Credenciales incorrectas. Verifique su usuario y contraseña.', 'danger')
                return redirect(url_for('login'))
                
        except Exception as e:
            flash(f'Error al conectar con la base de datos: {str(e)}', 'danger')
            return redirect(url_for('login'))
            
    return render_template('login.html')

# 5. Ruta para Guardar el Reclamo en PostgreSQL + PostGIS (CON FOTO)
@app.route('/guardar-reclamo', methods=['POST'])
def guardar_reclamo():
    if not session.get('usuario_id'):
        flash('Debe iniciar sesión para reportar una incidencia.', 'danger')
        return redirect(url_for('login'))

    usuario_id = session.get('usuario_id')
    tipologia_id = request.form.get('tipologia_id')
    prioridad = request.form.get('prioridad')
    descripcion = request.form.get('descripcion')
    latitud = request.form.get('latitud')
    longitud = request.form.get('longitud')
    
    # Capturamos el archivo de la foto (si es que el usuario subio una)
    foto = request.files.get('foto')

    if not latitud or not longitud:
        flash('Debe marcar la ubicación exacta en el mapa.', 'warning')
        return redirect(url_for('nuevo_reclamo'))

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # usuario para la auditoría
        cur.execute("SELECT set_config('app.user_id', %s, true);", (str(usuario_id),))

        # el ticket base
        cur.execute("""
            INSERT INTO tickets (usuario_id, tipologia_id, prioridad, descripcion, ubicacion)
            VALUES (%s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
            RETURNING id;
        """, (usuario_id, tipologia_id, prioridad, descripcion, longitud, latitud))
        
        nuevo_ticket_id = cur.fetchone()[0]

        # PROCESAMIENTO DE LA FOTOGRAFÍA
        if foto and foto.filename != '' and allowed_file(foto.filename):
            # Extraemos la extensión original (ej: .jpg)
            extension = foto.filename.rsplit('.', 1)[1].lower()
            # Renombramos el archivo para que sea único
            nombre_archivo = f"ticket_{nuevo_ticket_id}.{extension}"
            
            # Guardamos el archivo físicamente en el servidor
            ruta_fisica = os.path.join(app.config['UPLOAD_FOLDER'], nombre_archivo)
            foto.save(ruta_fisica)
            
            # Guardamos la ruta web en la tabla ticket_fotos
            ruta_web = f"/static/uploads/{nombre_archivo}"
            cur.execute("""
                INSERT INTO ticket_fotos (ticket_id, url)
                VALUES (%s, %s);
            """, (nuevo_ticket_id, ruta_web))

        conn.commit()
        cur.close()
        conn.close()

        flash(f'¡Reporte enviado correctamente! Tu Ticket ID es: #{nuevo_ticket_id}', 'success')
        return redirect(url_for('mis_tickets'))

    except Exception as e:
        flash(f'Error al registrar el reclamo: {str(e)}', 'danger')
        return redirect(url_for('nuevo_reclamo'))
    

# 6. Ruta para que el Ciudadano vea sus tickets (CU04)
@app.route('/mis-tickets')
def mis_tickets():
    # Validamos que esté logueado y sea Ciudadano
    if not session.get('usuario_id') or session.get('rol') != 'Ciudadano':
        flash('Acceso denegado. Esta sección es exclusiva para ciudadanos.', 'danger')
        return redirect(url_for('login'))
        
    usuario_id = session.get('usuario_id')
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Buscamos los tickets de este usuario en particular, trayendo los nombres de tipología y estado
        cur.execute("""
            SELECT t.id, tip.nombre, t.descripcion, t.fecha_creacion, e.nombre as estado, t.prioridad
            FROM tickets t
            JOIN tipologias tip ON t.tipologia_id = tip.id
            JOIN estados_ticket e ON t.estado_id = e.id
            WHERE t.usuario_id = %s
            ORDER BY t.fecha_creacion DESC;
        """, (usuario_id,))
        
        tickets = cur.fetchall()
        cur.close()
        conn.close()
        
        return render_template('mis_tickets.html', tickets=tickets)
        
    except Exception as e:
        flash(f'Error al cargar los tickets: {str(e)}', 'danger')
        return redirect(url_for('index'))

# 7. Ruta para la Bandeja de Tickets (Administrador / Operador)
@app.route('/bandeja-tickets')
def bandeja_tickets():
    if session.get('rol') not in ['Administrador', 'Operador']:
        flash('Acceso denegado. Área exclusiva para personal municipal.', 'danger')
        return redirect(url_for('index'))
    
    # Capturamos el filtro de la URL (ej: /bandeja-tickets?estado=Pendiente)
    estado_filtro = request.args.get('estado')
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Armamos la consulta SQL base
        query = """
            SELECT t.id, t.fecha_creacion, tip.nombre, t.prioridad, e.nombre as estado, u.nombre_completo
            FROM tickets t
            JOIN tipologias tip ON t.tipologia_id = tip.id
            JOIN estados_ticket e ON t.estado_id = e.id
            JOIN usuarios u ON t.usuario_id = u.id
        """
        parametros = ()
        
        # Si el usuario seleccionó un filtro, agregamos la condición WHERE
        if estado_filtro:
            query += " WHERE e.nombre = %s"
            parametros = (estado_filtro,)
            
        # Siempre ordenamos por los mas nuevos primero
        query += " ORDER BY t.fecha_creacion DESC;"
        
        cur.execute(query, parametros)
        tickets = cur.fetchall()
        cur.close()
        conn.close()
        
        # Enviamos los tickets y el estado actual para que el HTML sepa qué botón pintar
        return render_template('bandeja_tickets.html', tickets=tickets, estado_actual=estado_filtro)
        
    except Exception as e:
        flash(f'Error al cargar la bandeja: {str(e)}', 'danger')
        return redirect(url_for('dashboard'))

# 8. Ruta para ver el detalle de un ticket específico (CU07 y CU08)
@app.route('/ticket/<int:id>')
def detalle_ticket(id):
    # Validamos que solo el personal autorizado pueda entrar
    if session.get('rol') not in ['Administrador', 'Operador']:
        flash('Acceso denegado.', 'danger')
        return redirect(url_for('index'))

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        
        cur.execute("""
            SELECT t.id, t.fecha_creacion, tip.nombre, t.prioridad, e.nombre as estado,
                   u.nombre_completo, u.dni, u.correo, t.descripcion,
                   ST_Y(t.ubicacion::geometry) as latitud, ST_X(t.ubicacion::geometry) as longitud
            FROM tickets t
            JOIN tipologias tip ON t.tipologia_id = tip.id
            JOIN estados_ticket e ON t.estado_id = e.id
            JOIN usuarios u ON t.usuario_id = u.id
            WHERE t.id = %s;
        """, (id,))
        ticket = cur.fetchone()

        # buscamos la lista de cuadrillas activas y traemos todas las cuadrillas para el menú desplegable
        cur.execute("SELECT id, nombre FROM cuadrillas;")
        cuadrillas = cur.fetchall()

        cur.close()
        conn.close()

        if not ticket:
            flash('El ticket solicitado no existe.', 'warning')
            return redirect(url_for('bandeja_tickets'))

        return render_template('detalle_ticket.html', ticket=ticket, cuadrillas=cuadrillas)

    except Exception as e:
        flash(f'Error al cargar el detalle del ticket: {str(e)}', 'danger')
        return redirect(url_for('bandeja_tickets'))

# 9. Ruta para asignar cuadrilla (CU07)
@app.route('/ticket/<int:id>/asignar', methods=['POST'])
def asignar_cuadrilla(id):
    if session.get('rol') not in ['Administrador', 'Operador']:
        return redirect(url_for('index'))
    
    cuadrilla_id = request.form.get('cuadrilla_id')
    # Le avisamos a la base de datos quién está haciendo el cambio para tu auditoría
    usuario_id = session.get('usuario_id') 
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Le pasamos el ID del usuario actual a la configuración de Postgres
        cur.execute("SELECT set_config('app.user_id', %s, true);", (str(usuario_id),))
        
        cur.execute("""
            UPDATE tickets 
            SET cuadrilla_id = %s, 
                estado_id = (SELECT id FROM estados_ticket WHERE nombre = 'Asignado') 
            WHERE id = %s
        """, (cuadrilla_id, id))
        
        conn.commit()
        cur.close()
        conn.close()
        
        flash('Orden de trabajo emitida. El ticket ahora está "Asignado".', 'success')
        
    except Exception as e:
        flash(f'Error al asignar cuadrilla: {str(e)}', 'danger')
        
    return redirect(url_for('detalle_ticket', id=id))


# 10. Ruta para cambiar estado (CU08)
@app.route('/ticket/<int:id>/estado', methods=['POST'])
def cambiar_estado(id):
    if session.get('rol') not in ['Administrador', 'Operador']:
        return redirect(url_for('index'))
    
    nuevo_estado = request.form.get('nuevo_estado')
    usuario_id = session.get('usuario_id')
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Le pasamos el ID del usuario actual a la configuración de Postgres
        cur.execute("SELECT set_config('app.user_id', %s, true);", (str(usuario_id),))
        
        cur.execute("""
            UPDATE tickets 
            SET estado_id = (SELECT id FROM estados_ticket WHERE nombre = %s) 
            WHERE id = %s
        """, (nuevo_estado, id))
        
        conn.commit()
        cur.close()
        conn.close()
        
        flash(f'El estado del ticket se actualizó correctamente a: {nuevo_estado}', 'success')
        
    except Exception as e:
        flash(f'Error al cambiar el estado: {str(e)}', 'danger')
        
    return redirect(url_for('detalle_ticket', id=id))

# 11. Ruta para el Módulo de Auditoría
@app.route('/auditoria')
def auditoria():
    if session.get('rol') != 'Administrador':
        flash('Acceso denegado. Módulo exclusivo.', 'danger')
        return redirect(url_for('dashboard'))
        
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Leemos la tabla auditoria_avanzada, extrayendo info útil del JSON
        cur.execute("""
            SELECT 
                a.id, 
                a.fecha, 
                a.registro_id, 
                a.operacion, 
                u.nombre_completo,
                a.tabla_afectada
            FROM auditoria_avanzada a
            LEFT JOIN usuarios u ON a.usuario_id = u.id
            ORDER BY a.fecha DESC
            LIMIT 100;
        """)
        
        logs = cur.fetchall()
        cur.close()
        conn.close()
        
        return render_template('auditoria.html', logs=logs)
        
    except Exception as e:
        flash(f'Error al cargar logs: {str(e)}', 'danger')
        return render_template('auditoria.html', logs=[])

# 12. Ruta para descargar el informe de auditoría en formato PDF
from flask import make_response
from fpdf import FPDF

@app.route('/auditoria/pdf')
def auditoria_pdf():
    # Validamos que solo el Administrador pueda descargar los logs
    if session.get('rol') != 'Administrador':
        flash('Acceso denegado.', 'danger')
        return redirect(url_for('dashboard'))
        
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Buscamos los datos directamente de tu tabla auditoria_avanzada
        cur.execute("""
            SELECT a.id, a.fecha, a.registro_id, a.operacion, u.nombre_completo, a.tabla_afectada
            FROM auditoria_avanzada a
            LEFT JOIN usuarios u ON a.usuario_id = u.id
            ORDER BY a.fecha DESC
            LIMIT 100;
        """)
        logs = cur.fetchall()
        cur.close()
        conn.close()
        
        # Iniciamos la creación del documento PDF
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Arial", "B", 16)
        
        # Encabezado del informe
        pdf.cell(0, 10, "MUNICIPIO DE VILLA ANGELA", ln=True, align="C")
        pdf.set_font("Arial", "", 12)
        pdf.cell(0, 10, "Secretaria de Servicios Publicos - Informe de Auditoria", ln=True, align="C")
        pdf.ln(10)
        
        # Configuración de la tabla: Encabezados
        pdf.set_font("Arial", "B", 10)
        pdf.set_fill_color(240, 240, 240)
        
        pdf.cell(15, 10, "ID Log", 1, 0, "C", True)
        pdf.cell(40, 10, "Fecha y Hora", 1, 0, "C", True)
        pdf.cell(25, 10, "Ref. ID", 1, 0, "C", True)
        pdf.cell(30, 10, "Operacion", 1, 0, "C", True)
        pdf.cell(45, 10, "Responsable", 1, 0, "C", True)
        pdf.cell(35, 10, "Tabla", 1, 1, "C", True)
        
        # Contenido de la tabla
        pdf.set_font("Arial", "", 9)
        for log in logs:
            fecha_str = log[1].strftime('%d/%m/%Y %H:%M:%S')
            usuario_str = str(log[4]) if log[4] else 'Sistema'
            
            pdf.cell(15, 8, f"#{log[0]}", 1, 0, "C")
            pdf.cell(40, 8, fecha_str, 1, 0, "C")
            pdf.cell(25, 8, f"#{log[2]}", 1, 0, "C")
            pdf.cell(30, 8, str(log[3]), 1, 0, "C")
            pdf.cell(45, 8, usuario_str, 1, 0, "L")
            pdf.cell(35, 8, str(log[5]), 1, 1, "C")
            
        # 1. Obtenemos la salida del PDF
        pdf_output = pdf.output()
        
        # 2. Convertimos los datos a un formato de bytes seguro según la versión de fpdf2
        if isinstance(pdf_output, bytearray):
            pdf_bytes = bytes(pdf_output)
        elif isinstance(pdf_output, str):
            pdf_bytes = pdf_output.encode('latin1')
        else:
            pdf_bytes = pdf_output

        # 3. Armamos la respuesta indicando el tamaño exacto del archivo (Content-Length)
        response = make_response(pdf_bytes)
        response.headers['Content-Type'] = 'application/pdf'
        response.headers['Content-Disposition'] = 'attachment; filename=reporte_auditoria.pdf'
        response.headers['Content-Length'] = str(len(pdf_bytes)) # <-- Esto soluciona el "Archivo incompleto"
        
        return response
        
    except Exception as e:
        flash(f'Error al generar el archivo PDF: {str(e)}', 'danger')
        return redirect(url_for('auditoria'))
        
# 13. Ruta para el Módulo de Seguimiento (Visor GIS)
@app.route('/seguimiento')
def seguimiento():
    if session.get('rol') not in ['Administrador', 'Operador']:
        flash('Acceso denegado.', 'danger')
        return redirect(url_for('index'))
        
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Consulta para extraer coordenadas limpias de PostGIS
        cur.execute("""
            SELECT t.id, tip.nombre as tipologia, t.prioridad, e.nombre as estado, t.descripcion,
                   ST_Y(t.ubicacion::geometry) as latitud, ST_X(t.ubicacion::geometry) as longitud
            FROM tickets t
            JOIN tipologias tip ON t.tipologia_id = tip.id
            JOIN estados_ticket e ON t.estado_id = e.id
            WHERE e.nombre NOT IN ('Resuelto', 'Desestimado') AND t.activo = TRUE;
        """)
        
        tickets_gis = cur.fetchall()
        cur.close()
        conn.close()
        
        return render_template('seguimiento.html', tickets=tickets_gis)
        
    except Exception as e:
        flash(f'Error al cargar el visor: {str(e)}', 'danger')
        return redirect(url_for('dashboard'))
    
# 14. Ruta para el Módulo de Métricas SLA 
@app.route('/metricas')
def metricas():
    if session.get('rol') not in ['Administrador', 'Operador']:
        flash('Acceso denegado. Módulo exclusivo para personal municipal.', 'danger')
        return redirect(url_for('index'))
        
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # 1. Actualizamos la vista de la BD para capturar los últimos movimientos
        cur.execute("REFRESH MATERIALIZED VIEW mv_dashboard_indicadores;")
        
        # 2. Consultamos los indicadores analíticos de la tabla
        cur.execute("""
            SELECT tipologia, total_tickets, resueltos, pendientes, sla_cumplido, sla_incumplido
            FROM mv_dashboard_indicadores
            ORDER BY total_tickets DESC;
        """)
        indicadores = cur.fetchall()
        
        cur.close()
        conn.close()
        
        return render_template('metricas.html', indicadores=indicadores)
        
    except Exception as e:
        flash(f'Error al cargar el módulo analítico SLA: {str(e)}', 'danger')
        return redirect(url_for('dashboard'))

# 16. Ruta para el Registro de Nuevos Ciudadanos
from werkzeug.security import generate_password_hash

@app.route('/registro', methods=['GET', 'POST'])
def registro():
    # Si el usuario ya inició sesión, lo redirigimos según su rol
    if session.get('usuario_id'):
        return redirect(url_for('dashboard') if session.get('rol') != 'Ciudadano' else url_for('mis_reclamos'))

    if request.method == 'POST':
        nombre_completo = request.form.get('nombre_completo')
        dni = request.form.get('dni')
        correo = request.form.get('correo')
        password = request.form.get('password')
        
        # Encriptamos la contraseña antes de guardarla para cumplir con password_hash
        password_encritada = generate_password_hash(password)
        
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            
            # Insertamos los datos. El rol se asignará automáticamente como 'Ciudadano' por el DEFAULT de la BD
            cur.execute("""
                INSERT INTO usuarios (nombre_completo, dni, correo, password_hash)
                VALUES (%s, %s, %s, %s);
            """, (nombre_completo, dni, correo, password_encritada))
            
            conn.commit()
            cur.close()
            conn.close()
            
            flash('Cuenta creada con éxito. Ya podés iniciar sesión.', 'success')
            return redirect(url_for('login')) # Redirige a la pantalla de login que ya tengas armada
            
        except Exception as e:
            # Si salta una excepción por los índices UNIQUE de DNI o Correo, avisamos al usuario
            flash('Error al registrarse. Es posible que el DNI o el correo ya se encuentren vinculados a otra cuenta.', 'danger')
            
    return render_template('registro.html')

# Ruta para Cerrar Sesión
@app.route('/logout')
def logout():
    session.clear()  # Borra absolutamente todos los datos de la sesión actual
    flash('Sesión cerrada correctamente.', 'success')  # Deja un aviso de éxito
    return redirect(url_for('index'))  # Te manda de vuelta a la portada pública


if __name__ == '__main__':
    # El modo debug=True hace que el servidor se reinicie solo cada vez que modificás el código
    app.run(debug=True, port=5000)
