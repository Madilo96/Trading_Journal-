# Trading Journal — Instrucciones de Deploy

## Archivos del proyecto
```
trading-journal/
├── app.py              ← Backend Flask
├── requirements.txt    ← Dependencias Python
├── render.yaml         ← Config para Render
└── static/
    └── index.html      ← Frontend
```

## Deploy en Render (gratis, sin tarjeta)

### 1. Sube el código a GitHub
1. Crea cuenta en github.com si no tienes
2. Crea un repositorio nuevo llamado `trading-journal`
3. Sube los archivos (arrastra y suelta en la interfaz web de GitHub)

### 2. Despliega en Render
1. Ve a render.com y crea cuenta (con tu GitHub)
2. Clic en **New → Web Service**
3. Conecta tu repositorio `trading-journal`
4. Render detecta automáticamente el `render.yaml`
5. En **Environment Variables** agrega:
   - Key: `ANTHROPIC_API_KEY`
   - Value: tu API key de Anthropic (console.anthropic.com)
6. Clic **Deploy**

### 3. Listo
En ~2 minutos tendrás una URL tipo `https://trading-journal-xxxx.onrender.com`
Ábrela desde el celular o PC.

## Nota sobre el tier gratuito
El servicio se "duerme" tras 15 min de inactividad.
La primera vez que lo abras después puede tardar ~30 segundos.
Esto es normal y gratuito.

## Uso local (opcional)
```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=tu_key_aqui
python app.py
# Abre http://localhost:5000
```
