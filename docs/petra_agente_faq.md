# FAQ Agente Petra — knowledge base del bot

Este archivo es la fuente de verdad de las preguntas frecuentes que el bot de
WhatsApp responde a las Agentes Petra. Se carga automáticamente en
`petra_contenidos` (tipo='faq') vía migración idempotente en schema.sql.

**Reglas de redacción**:
- Respuestas cortas (2-5 líneas). WhatsApp friendly.
- Voseo rioplatense (vos, tenés, mandame).
- Negrita con `*texto*` para énfasis clave.
- Emoji con mesura, 1 por mensaje máximo.
- Si no sabés la respuesta exacta, decí "déjame averiguar y te confirmo".

---

## 1. Qué es Petra

### 1.1 ¿Qué es Petra?
Petra es una plataforma inmobiliaria que te conecta con interesados reales que buscan comprar o arrendar en *tu edificio*. Vos sos la cara visible: cuando alguien escanea el QR del lobby, llega a tu WhatsApp.

### 1.2 ¿Cómo gano dinero?
Cobrás *comisión* por cada operación que se cierra con un lead que viniste trabajando: 1% del valor de venta o un % del arriendo (según el plan de tu edificio). El pago llega a la cuenta que nos pases.

### 1.3 ¿Cuánto me va a tomar al día?
Lo que vos quieras. Mínimo: responder las citas que te lleguen por WhatsApp (3-5 min cada una). Si querés más ingresos, podés cargar inmuebles del edificio proactivamente.

### 1.4 ¿Me tengo que mudar o cambiar de trabajo?
No. Petra está pensada para que sumes ingresos *sin dejar lo que estás haciendo*. Todo pasa por WhatsApp, en tus tiempos.

### 1.5 ¿Puedo ser agente de varios edificios?
Por ahora *no*. Un edificio = una agente. Es parte de lo que hace confiable el modelo para los admins.

### 1.6 ¿Puedo dejar de ser agente cuando quiera?
Sí. Escribime *"ya no quiero ser agente"* y damos de baja tu perfil sin letra chica. Si cambiás de idea, podés volver después.

### 1.7 ¿Petra me cobra algo a mí?
*No*. Para la agente es *100% gratis*. Vos ganás comisión, no pagás nada.

### 1.8 ¿Petra le cobra al edificio?
No. Petra es *gratis para el edificio*. El admin solo da el ok para pegar un QR en el lobby. No hay cuotas ni letra chica.

### 1.9 ¿Qué hace Petra con mi información?
Solo la usamos para operar la plataforma (procesar leads, calcular comisiones, enviarte citas). No la vendemos ni la compartimos. Está protegida bajo la Ley 1581 de Colombia.

### 1.10 ¿Puedo usar Petra desde mi celular normal?
Sí. Todo pasa por WhatsApp, no hay app para descargar. Si tenés WhatsApp, podés ser Agente Petra.

---

## 2. Inscripción

### 2.1 ¿Qué datos necesitan para inscribirme?
Solo 5: nombre completo, cédula, ciudad, barrio, y nombre del conjunto donde vivís. Nada más — ni comprobante de ingresos ni papeles.

### 2.2 ¿Para qué me piden la cédula?
Para registrarte como Agente Petra y poder pagarte comisiones en regla. No la compartimos con el admin ni con nadie fuera del equipo.

### 2.3 ¿Puedo cambiar los datos después de inscribirme?
Sí. Escribime *"quiero corregir mi barrio"* (o el dato que sea) y lo cambiamos al toque.

### 2.4 Me equivoqué de edificio al inscribirme
Decime *"quiero cambiar de edificio"* y te dejo arrancar de nuevo con el conjunto correcto.

### 2.5 ¿Cuánto tarda la inscripción?
Menos de 2 minutos. 5 datos y listo.

### 2.6 Puse mal mi nombre, ¿cómo lo arreglo?
Escribime *"corregir nombre"* y te pido el nombre correcto.

### 2.7 ¿Dónde ven mis datos?
Solo el equipo de Petra y vos. No se publican, no los ven otros agentes ni el admin del edificio.

---

## 3. Capacitación

### 3.1 ¿Cuántos videos son?
Son *14 videos cortos*, 2 por día durante 7 días.

### 3.2 ¿Cuánto duran?
Entre 2 y 5 minutos cada uno. En total menos de 1 hora en toda la semana.

### 3.3 ¿Puedo verlos todos de una?
Si querés avanzar más rápido, escribime *"mandame el siguiente día"* después de ver los 2 de hoy y te mando los próximos sin esperar al cron de las 8am.

### 3.4 ¿Y si no entiendo algo del video?
Preguntame por acá, en texto. Si no puedo resolverlo, te lo paso a alguien del equipo.

### 3.5 No me llegaron los videos de hoy
Escribime *"reenviar videos"* y te los mando de nuevo.

### 3.6 ¿Puedo volver a ver un video pasado?
Sí. Decime *"quiero ver el video del día X"* y te lo re-envío.

### 3.7 ¿Qué pasa si me salto un día?
Nada grave. Cuando quieras seguir, escribime *"listo"* y te mando el próximo día pendiente.

### 3.8 ¿Me evalúan después de los videos?
No hay examen. Son para que arranques sabiendo lo mínimo. Después aprendés más haciendo.

### 3.9 ¿Puedo invitar a otra persona a verlos?
Los videos son para vos como agente. Si querés que otra persona vea algo específico, decime y vemos.

### 3.10 ¿Cuándo termino la capacitación?
En 7 días si ves 2 por día. Después pasás a la fase de pedirle autorización al admin.

---

## 4. Autorización del admin del conjunto

### 4.1 ¿Qué le digo al admin para que me autorice?
Decile 3 cosas:
1. Petra es *gratis para el edificio* (no cobra ni mantenimiento ni cuota).
2. Solo necesita permiso para *pegar un QR* chico en el lobby/portería.
3. Que vos vas a atender a los interesados, el edificio no se preocupa de nada.

### 4.2 El admin dice que no, ¿qué hago?
Decime *"el admin dijo que no"* y vemos: a veces solo le falta más info, otras veces es tema de que lo tenga que pasar a asamblea. Si no hay manera, podés cambiar de edificio.

### 4.3 El admin pregunta cuánto cuesta
*Nada*. Petra es gratis para el edificio. Vos ganás comisión, el edificio no paga un peso.

### 4.4 El admin dice que lo tiene que pasar a asamblea
Perfecto, es normal. Escribime *"pausar 15 días"* y te espero sin molestar. Cuando se defina, me avisás.

### 4.5 Al admin le preocupa la seguridad / los datos
El QR solo abre un formulario público (nombre + teléfono + qué busca). *No toca datos del edificio*, no accede a la portería ni a información de residentes. Es como pegar un cartel con un número.

### 4.6 ¿Puedo activar el QR sin autorización del admin?
No. Necesitamos el ok del admin porque el QR va pegado en una zona común del edificio. Sin eso no te podemos enviar el kit.

### 4.7 Soy el admin del edificio
¡Mejor! Escribime *"soy el admin del edificio"* y agilizamos el proceso.

### 4.8 No tengo contacto directo con el admin
Decime y vemos: podemos mandarte un mensaje tipo para que lo reenvíes al grupo del edificio o al portero.

### 4.9 ¿Petra puede hablar con el admin directamente?
Prefiero que lo hagas vos porque conocés al admin y es tu edificio. Si el tema se traba, podemos mandar algo escrito que vos le reenvíes.

### 4.10 ¿Qué pasa si el admin se cambia?
Si el nuevo admin pide revalidar, me avisás y le pasamos el material de nuevo.

### 4.11 ¿Me pueden mostrar casos de otros edificios?
Sí. Escribime *"quiero caso"* y te paso un ejemplo real.

### 4.12 ¿Y si el admin dice "me parece bien pero no ahora"?
Normal. Escribime *"pausar 15 días"* y te vuelvo a escribir en 2 semanas. Sin presionarte entre medio.

---

## 5. Kit QR + envío

### 5.1 ¿Qué incluye el kit?
Un *QR físico* para pegar en el lobby del edificio + *un regalito* 💜 de bienvenida.

### 5.2 ¿Cuánto tarda en llegar?
*2 a 3 días hábiles* desde que me pasás la dirección.

### 5.3 ¿A qué dirección lo mandan?
A la que me pases cuando el admin te autorice. Puede ser tu apto, una casa o el edificio directo.

### 5.4 ¿Dónde pego el QR?
Donde más lo vea la gente que pasa: *portería, lobby o ascensor*. A la altura de los ojos, bien visible.

### 5.5 ¿Me cobran por el kit o el envío?
No. Todo *gratis*. Nosotros lo mandamos.

### 5.6 El kit no me llegó aún
Si pasaron más de 4 días hábiles, decime *"no llegó el kit"* y lo rastreo.

### 5.7 Rompí el QR, ¿qué hago?
Decime *"necesito otro QR"* y te mandamos uno nuevo sin costo. Mientras tanto podés usar el QR digital que ya tenés por el link.

### 5.8 ¿Y si me mudo del edificio?
Avisame. Si te mudás a otro conjunto, lo migramos. Si dejás el barrio, podemos ponerte en pausa hasta que decidas.

### 5.9 ¿Puedo imprimir el QR mientras me llega el físico?
Sí. El link del QR digital te lo mandé por acá — podés imprimirlo y pegarlo mientras llega el físico.

### 5.10 ¿Cómo sé que el QR funciona?
Después de pegarlo, mandame una *foto del QR instalado* y lo verificamos. Apenas confirmo que anda, te activo oficialmente.

---

## 6. Comisiones y pagos

### 6.1 ¿Cuánto gano por una venta?
*1% del valor de venta*. En un apto de 500 millones son 5 millones de comisión para vos.

### 6.2 ¿Cuánto gano por un arriendo?
Un *% del canon mensual*. Varía según el edificio y el tipo de inmueble.

### 6.3 ¿Cuándo me pagan?
Apenas se concreta la operación y el propietario recibe la plata. Normalmente *dentro de los 5 días hábiles*.

### 6.4 ¿Cómo me pagan?
Por *transferencia bancaria* o Nequi/Daviplata/Bancolombia a la cuenta que vos nos pases. Vos elegís.

### 6.5 ¿Puedo ver mi plata acumulada?
Sí. Pronto vas a tener un panel propio donde ver todo. Mientras, decime *"cuánto tengo"* y te paso el detalle.

### 6.6 ¿Me cobran impuestos?
Si tus ingresos como agente superan el tope del régimen simplificado, sí hay retención. Te podemos ayudar a entender cuándo y cuánto.

### 6.7 ¿Hay un monto mínimo para retirar?
No. Lo que generes se te paga al cierre de la operación, sin mínimos.

### 6.8 ¿Qué pasa si el lead cerró conmigo pero por fuera de Petra?
Si el lead vino por tu QR y lo podemos probar (están los chats), la comisión es tuya igual. Avisanos y lo gestionamos.

### 6.9 ¿Puedo compartir comisión con otro?
No. La comisión va a la Agente Petra del edificio donde está el inmueble.

### 6.10 ¿Qué pasa si una venta se cae?
No se cobra comisión porque no hubo operación. Si vuelve a reactivarse, sí.

---

## 7. Inmuebles / inventario

### 7.1 ¿Qué inmuebles puedo cargar?
Los de *tu edificio* que estén en *venta o arriendo*. Necesitás el ok del propietario.

### 7.2 ¿Necesito permiso del propietario para publicarlo?
Sí. Es el propietario el que decide qué publicás y a qué precio.

### 7.3 ¿Puedo cargar inmuebles de otros edificios?
No. Cada agente opera *solo su edificio*. Mantiene el modelo limpio.

### 7.4 ¿Qué fotos pongo?
Fotos del *inmueble como está* — sin filtros ni montajes. Ideal: 5-8 fotos (sala, cuartos, baño, cocina, vista si hay).

### 7.5 ¿Puedo cambiar el precio después?
Sí. Decime *"quiero cambiar el precio del inmueble X"* y lo actualizo.

### 7.6 ¿Cuántos inmuebles puedo cargar?
Los que tengas. No hay límite.

### 7.7 ¿Qué datos pide Petra del inmueble?
10 campos: modalidad (venta/arriendo), torre, apto, área m², habitaciones, baños, parqueadero, precio, nombre y teléfono del propietario.

### 7.8 El propietario pide que no se vea su número
Lo manejamos nosotros. Vos sos la intermediaria — los interesados te escriben a vos primero.

### 7.9 ¿Puedo despublicar un inmueble?
Sí. Decime *"quitar inmueble X"* y queda fuera del catálogo.

### 7.10 ¿Un inmueble puede estar en Petra y en otro portal a la vez?
Si el propietario lo permite, sí. No tenemos exclusividad.

---

## 8. Citas / visitas

### 8.1 ¿Cómo me llegan las citas?
Te llega un mensaje por acá con los datos del interesado y la hora propuesta. Vos aceptás, rechazás o proponés otra hora.

### 8.2 No puedo esa hora, ¿qué hago?
Pulsá *"Proponer otra hora"* y mandame día/hora alternativo (ej. *jueves 3pm*).

### 8.3 El interesado nunca llegó
Decime *"no vino el interesado de la cita X"* y lo marcamos como no-show.

### 8.4 ¿Tengo que acompañar al interesado durante la visita?
Sí. La idea es que lo recibas, le muestres el inmueble y respondas preguntas básicas. Si pregunta algo técnico que no sepás, podés decir "te lo confirmo".

### 8.5 ¿Y si cancelo la visita?
Avisame apenas puedas y le reagendamos al interesado. Un cancelado no es grave; una serie de cancelados sí.

### 8.6 ¿Cuánto me demora una visita?
Entre 20 y 40 minutos: recibirlo, mostrarle, responder dudas y despedirse.

### 8.7 ¿Petra me paga por hacer la visita?
No por la visita en sí. Cobrás solo si la operación se cierra (venta o arriendo).

### 8.8 El interesado quiere ver otra unidad diferente
Si tenés el inmueble cargado, perfecto. Si no, decime y lo buscamos.

### 8.9 ¿Puedo pedir que no me manden citas tal día?
Sí. Decime *"no disponible el sábado"* o lo que sea y bloqueo esas horas.

---

## 9. Legal / privacidad / contrato

### 9.1 ¿Firmo contrato?
Vas a firmar un *acuerdo de agente* digital (click en WhatsApp cuenta). Sin ir a notaría. Define que sos Agente Petra autorizada y cómo se calculan tus comisiones.

### 9.2 ¿Qué pasa con mis datos personales?
Están protegidos por la *Ley 1581 de 2012* (habeas data Colombia). Solo los usa Petra, no se venden ni se comparten. Podés pedir que los borremos cuando dejes de ser agente.

### 9.3 Petra, ¿se queda con mi inmueble si yo lo cargo?
No. El inmueble sigue siendo del propietario. Nosotros solo lo publicamos y gestionamos los leads.

### 9.4 ¿Puedo competir con otras agentes?
No competís directamente. Cada edificio tiene UNA agente. Si otro conjunto tiene su agente, ustedes trabajan en zonas distintas.

### 9.5 Si el propietario me reclama, ¿qué pasa?
Nosotros te respaldamos: tenés los chats, los datos del lead y el histórico de la operación. Si hay un conflicto real, lo resolvemos juntas.

### 9.6 ¿Qué pasa si doy mal una comisión al propietario?
Las comisiones las calcula Petra automáticamente. Si hubo un error, decímelo y lo corregimos.

### 9.7 ¿Puedo trabajar con una inmobiliaria tradicional a la vez?
Mientras no haya conflicto directo con tu edificio, sí. Avisanos si hay algún tema.

### 9.8 Soy menor de edad, ¿puedo ser agente?
Necesitás ser mayor de 18 años para firmar el acuerdo y recibir pagos.

### 9.9 Soy extranjero/a, ¿puedo ser agente?
Si tenés cédula de extranjería y cuenta bancaria en Colombia, sí.

### 9.10 ¿Qué pasa con el QR si doy de baja mi cuenta?
Lo retiramos del circuito. Si alguien escanea el QR después, le decimos que no está activa esa agente.

---

## 10. Técnico / soporte / problemas comunes

### 10.1 No me llegan mensajes de Petra
Chequeá que no me tengas bloqueado y que mi número esté guardado. Si sigue, escribime desde otro medio y lo revisamos.

### 10.2 Me equivoqué de teléfono al registrarme
Decime *"quiero cambiar mi teléfono"* y vemos cómo migrar tu cuenta sin perder progreso.

### 10.3 Quiero hablar con un humano
Escribime *"necesito hablar con alguien del equipo"* y alguien te contacta en el día.

### 10.4 ¿Puedo pausar la cuenta un tiempo?
Sí. Decime *"pausar X días"* y no te molesto hasta esa fecha. Después retomo donde quedamos.

### 10.5 Perdí el acceso a WhatsApp
Si cambiaste de celular pero mantenés el mismo número, no hay drama — tus mensajes siguen llegando. Si perdiste el número, escribime desde el nuevo y recuperamos tu cuenta.

### 10.6 ¿Puedo tener Petra en 2 celulares?
Como WhatsApp Business permite 1 número = 1 sesión, por ahora es 1 celular por agente.

### 10.7 Me llegan mensajes en inglés o mal escritos
Avisame — a veces mi tecnología se enreda. Te lo arreglo.

### 10.8 Veo publicidad de otro edificio en el chat
No debería pasar. Mandame screenshot y lo investigo.

### 10.9 El QR no me funciona cuando lo escaneo yo mismo
Normal — algunos celulares no abren tu propio QR si estás logueada. Pedile a alguien de tu casa que lo pruebe.

### 10.10 Mi WhatsApp está lleno y no me caben más chats
Petra solo necesita que tengas espacio para los chats nuevos de interesados. No ocupa más que un chat común.

### 10.11 ¿Mis mensajes se guardan?
Sí, los que son operativos (datos, citas, operaciones). Los no-operativos no se guardan más de lo que te mande atrás este chat.

### 10.12 ¿Hay horario de atención?
El bot está 24/7. Si pedís humano, respondemos en horario laboral (lunes a viernes 9-18, Colombia).

---

## 11. Cómo funciona el día a día (una vez activa)

### 11.1 ¿Qué pasa después de que el QR esté instalado?
Empezás a *recibir leads*. Cuando alguien escanea el QR del lobby, su mensaje te llega directo acá.

### 11.2 ¿Cómo convierto un lead en una operación?
Lo saludás, le preguntás qué busca, le mostrás un inmueble del edificio (o del edificio vecino si tiene agente aliado), coordinás una visita, y si le gusta, lo cerrás con el propietario.

### 11.3 ¿Qué le respondo a un interesado?
Petra te sugiere la primera respuesta, pero vos elegís el tono. La idea: amable, concreta, y que proponga un próximo paso (ver fotos, agendar visita).

### 11.4 ¿Qué pasa si un lead no responde?
Normal. De cada 10 leads, cerrás 1-2. El resto es parte del proceso.

### 11.5 ¿Cuántos leads voy a recibir?
Depende del edificio. En promedio: *3 a 8 leads por mes* en edificios activos.

### 11.6 ¿Y si no me siento cómoda vendiendo?
No necesitás ser comercial pro. Sé vos, honesta y clara. Es más efectivo que forzar un libreto de venta.

### 11.7 ¿Petra me sugiere qué decir?
Sí, en casos complejos. Para las respuestas comunes, armás tu estilo y el bot te lo recuerda.

### 11.8 ¿Qué hago si un interesado quiere financiación?
Petra tiene aliados de crédito hipotecario. Decime *"el interesado necesita crédito"* y te paso el contacto.

### 11.9 ¿Puedo ver cuántas visitas hice este mes?
Pronto en tu panel propio. Mientras, decime *"cuántas visitas tengo"* y te paso el conteo.

### 11.10 ¿Qué pasa si me va bien y quiero crecer?
Si cerrás varias operaciones y querés escalar, podemos conversar: hay programas para agentes top (más edificios, más herramientas, pagos más rápidos).
