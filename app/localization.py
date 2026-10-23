"""Reviewed English/Spanish copy for every customer-facing backend string.

One dictionary with stable semantic keys; the English column is the single
source for the constants re-exported by ``app.prompts``. Templates keep the
contract placeholder names (``{field label}``, ``{session_id}``, ...) so the
English rendering stays byte-identical to the implementation contract.
"""

from __future__ import annotations

from app.schemas import FieldName, Language, Slots

SUPPORTED_LANGUAGES: tuple[Language, ...] = ("en", "es")
DEFAULT_LANGUAGE: Language = "en"

STRINGS: dict[str, dict[Language, str]] = {
    "greeting": {
        "en": (
            "Hello! I can answer Northstar Shop policy questions or help record an order "
            "issue. How can I help? Please do not share payment details."
        ),
        "es": (
            "¡Hola! Puedo responder preguntas sobre las políticas de Northstar Shop o "
            "ayudarte a registrar un problema con un pedido. ¿En qué puedo ayudarte? "
            "No compartas datos de pago."
        ),
    },
    "social_acknowledgment": {
        "en": "Hello! How can I help with Northstar Shop?",
        "es": "¡Hola! ¿En qué puedo ayudarte con Northstar Shop?",
    },
    "capability_reply": {
        "en": "I can answer Northstar Shop policy questions or help record an order issue.",
        "es": (
            "Puedo responder preguntas sobre las políticas de Northstar Shop o ayudarte "
            "a registrar un problema con un pedido."
        ),
    },
    "nothing_to_confirm": {
        "en": (
            "There is nothing ready to confirm yet. Please describe the issue you would "
            "like to report."
        ),
        "es": (
            "Todavía no hay nada listo para confirmar. Describe el problema que quieres "
            "registrar."
        ),
    },
    "no_review_to_reject": {
        "en": (
            "There is no review to reject yet. Please describe the issue you would like "
            "to report."
        ),
        "es": (
            "Todavía no hay ninguna revisión que rechazar. Describe el problema que "
            "quieres registrar."
        ),
    },
    "missing_order": {
        "en": (
            "What is your order number? Use NS- followed by six digits, for example NS-123456."
        ),
        "es": (
            "¿Cuál es tu número de pedido? Usa NS- seguido de seis dígitos, por ejemplo "
            "NS-123456."
        ),
    },
    "missing_category": {
        "en": (
            "Is this about delivery, a damaged item, a wrong item, a return/refund, or "
            "something else?"
        ),
        "es": (
            "¿Se trata de la entrega, un artículo dañado, un artículo incorrecto, una "
            "devolución o reembolso, u otra cosa?"
        ),
    },
    "missing_description": {
        "en": "Please describe what happened in at least 10 characters.",
        "es": "Describe lo que ocurrió en al menos 10 caracteres.",
    },
    "missing_urgency": {
        "en": "How urgent is this: low, normal, or high?",
        "es": "¿Qué urgencia tiene: baja, normal o alta?",
    },
    "invalid_order": {
        "en": "Please use NS- followed by exactly six digits, for example NS-123456.",
        "es": "Usa NS- seguido de exactamente seis dígitos, por ejemplo NS-123456.",
    },
    "invalid_category": {
        "en": "Please choose delivery, damaged item, wrong item, return/refund, or other.",
        "es": (
            "Elige entrega, artículo dañado, artículo incorrecto, devolución o "
            "reembolso, u otro."
        ),
    },
    "invalid_description": {
        "en": (
            "Please describe the problem using 10 to 2000 characters, rather than only a "
            "category label."
        ),
        "es": (
            "Describe el problema con entre 10 y 2000 caracteres, no solo una etiqueta "
            "de categoría."
        ),
    },
    "invalid_urgency": {
        "en": "Please choose low, normal, or high urgency.",
        "es": "Elige urgencia baja, normal o alta.",
    },
    "conflict_template": {
        "en": (
            "You previously gave {old}. Should I keep that or use {candidate} for "
            "{field label}?"
        ),
        "es": (
            "Antes indicaste {old}. ¿Debo mantenerlo o usar {candidate} para "
            "{field label}?"
        ),
    },
    "ambiguous_template": {
        "en": "Please clarify your {field label}.",
        "es": "Aclara tu {field label}.",
    },
    "capability": {
        "en": (
            "I can record an order issue and summarize it. I cannot verify orders, issue "
            "refunds, or contact a support team."
        ),
        "es": (
            "Puedo registrar un problema con un pedido y resumirlo. No puedo verificar "
            "pedidos, emitir reembolsos ni contactar con un equipo de soporte."
        ),
    },
    "receipt": {
        "en": "Saved locally as {session_id}. No request has been sent to a real support team.",
        "es": (
            "Guardado localmente como {session_id}. No se ha enviado ninguna solicitud a "
            "un equipo de soporte real."
        ),
    },
    "limit": {
        "en": (
            "This intake has reached its message limit. You can confirm an existing review "
            "or start a new intake."
        ),
        "es": (
            "Este registro ha alcanzado su límite de mensajes. Puedes confirmar una "
            "revisión existente o iniciar un registro nuevo."
        ),
    },
    "reject_prompt": {
        "en": "What would you like to change?",
        "es": "¿Qué te gustaría cambiar?",
    },
    "review_suffix": {
        "en": "\nPlease confirm these details or tell me what to change.",
        "es": "\nConfirma estos datos o dime qué cambiar.",
    },
    "summary_template": {
        "en": "Order: {order_number}\nCategory: {category}\nProblem: {description}\nUrgency: {urgency}",
        "es": "Pedido: {order_number}\nCategoría: {category}\nProblema: {description}\nUrgencia: {urgency}",
    },
    "abstention": {
        "en": (
            "I do not have that information in the Northstar Shop knowledge base. "
            "I can still record your issue for review."
        ),
        "es": (
            "No tengo esa información en la base de conocimiento de Northstar Shop. "
            "Aún puedo registrar tu problema para revisión."
        ),
    },
    "policy_question_suffix": {
        "en": "Please ask your policy question in a separate message.",
        "es": "Haz tu pregunta sobre políticas en un mensaje aparte.",
    },
    "unsupported_language": {
        "en": (
            "This demo supports English and Spanish only. Please write in English or "
            "Spanish."
        ),
        "es": "Esta demostración solo admite inglés y español. Escribe en inglés o español.",
    },
    "language_switch_restate": {
        "en": (
            "I will continue in English. Please describe your order issue in English so I "
            "can record it."
        ),
        "es": (
            "Continuaré en español. Describe el problema de tu pedido en español para que "
            "pueda registrarlo."
        ),
    },
    "speech_unavailable": {
        "en": "Spoken playback is not available for this reply's language.",
        "es": "La reproducción de voz no está disponible en el idioma de esta respuesta.",
    },
    "field_order_number": {"en": "order number", "es": "número de pedido"},
    "field_category": {"en": "problem category", "es": "categoría del problema"},
    "field_description": {"en": "problem description", "es": "descripción del problema"},
    "field_urgency": {"en": "urgency", "es": "urgencia"},
}

MISSING_QUESTION_KEYS: dict[FieldName, str] = {
    "order_number": "missing_order",
    "category": "missing_category",
    "description": "missing_description",
    "urgency": "missing_urgency",
}

INVALID_QUESTION_KEYS: dict[FieldName, str] = {
    "order_number": "invalid_order",
    "category": "invalid_category",
    "description": "invalid_description",
    "urgency": "invalid_urgency",
}

FIELD_LABEL_KEYS: dict[FieldName, str] = {
    "order_number": "field_order_number",
    "category": "field_category",
    "description": "field_description",
    "urgency": "field_urgency",
}


def _check_language(language: str) -> Language:
    if language not in SUPPORTED_LANGUAGES:
        raise KeyError(language)
    return language


def text(key: str, language: str = DEFAULT_LANGUAGE) -> str:
    """Return the reviewed copy for ``key`` in ``language``."""

    entry = STRINGS[key]
    return entry[_check_language(language)]


def field_label(field: FieldName, language: str = DEFAULT_LANGUAGE) -> str:
    return text(FIELD_LABEL_KEYS[field], language)


def missing_question(field: FieldName, language: str = DEFAULT_LANGUAGE) -> str:
    return text(MISSING_QUESTION_KEYS[field], language)


def invalid_question(field: FieldName, language: str = DEFAULT_LANGUAGE) -> str:
    return text(INVALID_QUESTION_KEYS[field], language)


def conflict_question(
    field: FieldName, old: str, candidate: str, language: str = DEFAULT_LANGUAGE
) -> str:
    return text("conflict_template", language).format_map(
        {
            "old": old,
            "candidate": candidate,
            "field label": field_label(field, language),
        }
    )


def ambiguous_question(field: FieldName, language: str = DEFAULT_LANGUAGE) -> str:
    return text("ambiguous_template", language).format_map(
        {"field label": field_label(field, language)}
    )


def build_summary(slots: Slots, language: str = DEFAULT_LANGUAGE) -> str:
    values = {field: getattr(slots, field) for field in MISSING_QUESTION_KEYS}
    if any(value is None for value in values.values()):
        raise ValueError("summary requires all four fields")
    return text("summary_template", language).format(**values)


def build_review_reply(slots: Slots, language: str = DEFAULT_LANGUAGE) -> str:
    return build_summary(slots, language) + text("review_suffix", language)
