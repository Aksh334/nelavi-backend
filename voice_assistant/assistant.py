"""CropSync Agricultural Question Answering Assistant.
Integrates rover telemetry, soil sensing, research evidence, and agronomic knowledge.
Answers queries in English, Hindi, and Telugu with strict provenance and safety warnings.
"""

from typing import Dict, List, Optional, Tuple

from voice_assistant.boundary import (
    AssistantAnswer,
    AssistantQuestion,
    LANG_EN,
    LANG_HI,
    LANG_TE,
    MAX_QUESTION_CHARS,
    SUPPORTED_LANGUAGES,
)

ADVISORY_DISCLAIMERS = {
    LANG_EN: (
        "Disclaimer: This is automated agricultural guidance based on available CropSync telemetry and agronomic models. "
        "Consult your local agricultural extension officer or agronomist for field-level decisions."
    ),
    LANG_HI: (
        "अस्वीकरण: यह उपलब्ध क्रॉपसिंक टेलीमेट्री और कृषि विज्ञान मॉडल पर आधारित स्वचालित मार्गदर्शन है। "
        "खेत-स्तरीय निर्णयों के लिए हमेशा अपने स्थानीय कृषि विस्तार अधिकारी या विशेषज्ञ से सलाह लें।"
    ),
    LANG_TE: (
        "నిరాకరణ: ఇది అందుబాటులో ఉన్న క్రాప్ సింక్ రీడింగ్స్ మరియు వ్యవసాయ సూత్రాల ఆధారంగా అందించిన మార్గదర్శకత్వం. "
        "క్షేత్ర స్థాయి నిర్ణయాల కోసం ఎల్లప్పుడూ మీ స్థానిక వ్యవసాయ అధికారి లేదా వ్యవసాయ నిపుణుడిని సంప్రదించండి."
    ),
}

# Grounded Topic Keywords and Insights
TOPICS = {
    "moisture": {
        "keywords": ["moisture", "water", "irrigation", "dry", "wet", "drainage", "पानी", "नमी", "सिंचाई", "నీరు", "తేమ", "సాగునీరు"],
        LANG_EN: {
            "title": "Irrigation & Soil Moisture",
            "guidance": (
                "Maintain root zone soil moisture between 50% and 70% of field capacity for optimal root respiration. "
                "Avoid standing water for over 24 hours to prevent anaerobic root rot and pythium infections. "
                "Morning or late afternoon drip irrigation maximizes water efficiency and reduces foliar disease risk."
            )
        },
        LANG_HI: {
            "title": "सिंचाई और मिट्टी की नमी",
            "guidance": (
                "जड़ों के अच्छे विकास के लिए मिट्टी में 50% से 70% नमी का स्तर बनाए रखें। "
                "खेत में 24 घंटे से अधिक पानी खड़ा न रहने दें ताकि जड़ सड़न और फफूंद से बचा जा सके। "
                "सुबह या शाम के समय ड्रिप सिंचाई करने से पानी की बचत होती है और पत्तियों की बीमारियां कम होती हैं।"
            )
        },
        LANG_TE: {
            "title": "నీటి యాజమాన్యం & నేల తేమ",
            "guidance": (
                "వేర్ల శ్వాసక్రియకు అనుకూలంగా నేలలో 50% నుండి 70% వరకు తేమ శాతాన్ని నిర్వహించాలి. "
                "చేనులో 24 గంటలకు మించి నీరు నిలబడకుండా చూసుకోవాలి, లేకుంటే వేరుకుళ్లు తెగులు వస్తుంది. "
                "ఉదయం లేదా సాయంత్రం వేళల్లో డ్రిప్ ద్వారా నీరు పెట్టడం వల్ల ఆకులపై తెగుళ్లు వ్యాపించకుండా ఉంటాయి."
            )
        }
    },
    "nitrogen": {
        "keywords": ["nitrogen", "urea", "npk", "yellow", "chlorosis", "stunted", "यूरिया", "नाइट्रोजन", "पीली", "నత్రజని", "యూరియా", "పసుపు"],
        LANG_EN: {
            "title": "Nitrogen Management & Chlorosis",
            "guidance": (
                "Nitrogen deficiency typically manifests as uniform pale yellowing starting on older, lower leaves as mobile N "
                "is translocated to younger growth. For field correction, split applications of urea or ammonium nitrate "
                "timed to active tillering/branching are more efficient than heavy basal applications."
            )
        },
        LANG_HI: {
            "title": "नाइट्रोजन प्रबंधन और पीलापन",
            "guidance": (
                "नाइट्रोजन की कमी से सबसे पहले नीचे की पुरानी पत्तियां एकसमान पीली पड़ने लगती हैं। "
                "इसकी रोकथाम के लिए यूरिया को एक साथ डालने के बजाय 2-3 बार में बांटकर (किश्तों में) फसल की बढ़वार के समय दें।"
            )
        },
        LANG_TE: {
            "title": "నత్రజని యాజమాన్యం & ఆకుల పసుపు రంగు",
            "guidance": (
                "నత్రజని లోపం వల్ల మొదట కింద ఉన్న పాత ఆకులు లేత పసుపు రంగులోకి మారుతాయి. "
                "యూరియాను ఒకేసారి వేయకుండా పంట ఎదుగుదల దశల్లో రెండు లేదా మూడు దఫాలుగా వేయడం వల్ల ఎక్కువ ఫలితం ఉంటుంది."
            )
        }
    },
    "phosphorus": {
        "keywords": ["phosphorus", "dap", "root", "roots", "purple", "फास्फोरस", "जड़", "భాస్వరం", "వేర్లు", "డిఎపి"],
        LANG_EN: {
            "title": "Phosphorus & Root Development",
            "guidance": (
                "Phosphorus drives root establishment and seedling vigour. Deficiency presents as dark green foliage with purple or reddish "
                "pigmentation along leaf margins and slow root elongation. Cold soils (<15°C) reduce P uptake; band-apply soluble phosphate near the seed zone."
            )
        },
        LANG_HI: {
            "title": "फास्फोरस और जड़ विकास",
            "guidance": (
                "फास्फोरस जड़ों के फैलाव और मजबूत विकास के लिए आवश्यक है। इसकी कमी से पत्तियों के किनारे बैंगनी या लाल पड़ने लगते हैं। "
                "डीएपी (DAP) या सुपर फास्फेट का प्रयोग बुवाई के समय जड़ों के पास करें।"
            )
        },
        LANG_TE: {
            "title": "భాస్వరం & వేర్ల అభివృద్ధి",
            "guidance": (
                "భాస్వరం వేర్ల వ్యవస్థ బలంగా విస్తరించడానికి తోడ్పడుతుంది. దీని లోపం ఉన్నప్పుడు ఆకుల అంచులు ఊదా లేదా ఎరుపు రంగులోకి మారుతాయి. "
                "విత్తే సమయంలో విత్తనం దగ్గరగా భాస్వరం ఎరువులను (DAP లేదా SSP) వేయడం ఉత్తమం."
            )
        }
    },
    "potassium": {
        "keywords": ["potassium", "potash", "mop", "scorch", "margin", "पोटाश", "पोटेशियम", "పొటాష్", "పొటాషియం"],
        LANG_EN: {
            "title": "Potassium & Stress Resistance",
            "guidance": (
                "Potassium regulates plant water balance and stomatal opening. Deficiency shows up as marginal leaf scorch, firing on leaf tips, "
                "and weak lodging-prone stems. Adequate K enhances drought resistance and improves fruit/grain weight."
            )
        },
        LANG_HI: {
            "title": "पोटाश और रोग प्रतिरोधक क्षमता",
            "guidance": (
                "पोटाश पौधों में पानी के संतुलन और सूखे से लड़ने की क्षमता को बढ़ाता है। इसकी कमी से पत्तियों के किनारे झुलसे हुए जैसे दिखते हैं। "
                "एमओपी (MOP) का सही प्रयोग दानों के वजन और गुणवत्ता को बढ़ाता है।"
            )
        },
        LANG_TE: {
            "title": "పొటాషియం & పంట నాణ్యత",
            "guidance": (
                "పొటాషియం మొక్కలలో నీటి సమతుల్యతను మరియు రోగనిరోధక శక్తిని నియంత్రిస్తుంది. దీని లోపం వల్ల ఆకుల చివర్లు ఎండినట్లు కనిపిస్తాయి. "
                "పొటాష్ తగిన మోతాదులో వాడటం వల్ల గింజ బరువు, నాణ్యత పెరుగుతాయి."
            )
        }
    },
    "pests": {
        "keywords": ["pest", "insect", "disease", "spot", "blight", "fungus", "कीट", "रोग", "धब्बा", "फफूंद", "తెగులు", "పురుగు", "మచ్చలు"],
        LANG_EN: {
            "title": "Integrated Pest & Disease Management",
            "guidance": (
                "Adopt Integrated Pest Management (IPM): scout regularly, encourage predatory beneficial insects, use neem-based botanicals at early infestation, "
                "and rotate fungicide modes of action to prevent resistance. Collect and destroy affected plant debris to break spore cycles."
            )
        },
        LANG_HI: {
            "title": "एकीकृत कीट और रोग प्रबंधन",
            "guidance": (
                "एकीकृत कीट प्रबंधन (IPM) अपनाएं: नियमित निरीक्षण करें, शुरुआती प्रकोप में नीम आधारित कीटनाशक का प्रयोग करें, "
                "और मित्र कीटों की रक्षा करें। संक्रमित पत्तियों को खेत से बाहर निकालकर नष्ट करें ताकि बीमारी आगे न फैले।"
            )
        },
        LANG_TE: {
            "title": "సమగ్ర సస్యరక్షణ & తెగుళ్ల నివారణ",
            "guidance": (
                "సమగ్ర సస్యరక్షణ (IPM) పద్ధతులు పాటించండి: చేనును ఎప్పటికప్పుడు గమనించండి, ప్రారంభ దశలో వేపనూనె లాంటి సేంద్రియ మందులు వాడండి. "
                "తెగులు సోకిన ఆకులను తీసివేసి నాశనం చేయడం ద్వారా తెగులు విస్తరించకుండా ఆపవచ్చు."
            )
        }
    }
}


class CropSyncAssistant:
    """Agricultural AI assistant connecting rover records, agronomic knowledge, and multilingual response formatting."""

    def __init__(self, record_store=None, research_client=None, synthesis_provider=None, findings_analyzer=None):
        self._record_store = record_store
        self._research_client = research_client
        self._synthesis_provider = synthesis_provider
        self._findings_analyzer = findings_analyzer

    def _get_latest_rover_context(self) -> Optional[dict]:
        """Fetch the latest rover sensor record if a record store is configured."""
        if not self._record_store:
            return None
        try:
            records = self._record_store.history(limit=1)
            if records and len(records) > 0:
                rec = records[0]
                soil = rec.get("soil", {})
                air = rec.get("air", {})
                sensors = rec.get("sensor_status", {})
                return {
                    "available": True,
                    "sequence_number": rec.get("sequence_number"),
                    "time": rec.get("time"),
                    "soil": {
                        "nitrogen": soil.get("nitrogen"),
                        "phosphorus": soil.get("phosphorus"),
                        "potassium": soil.get("potassium"),
                        "moisture": soil.get("moisture"),
                        "temperature": soil.get("temperature"),
                    },
                    "air": {
                        "temperature": air.get("temperature"),
                        "humidity": air.get("humidity"),
                    },
                    "sensor_status": sensors,
                    "battery_voltage": rec.get("battery", {}).get("voltage_v"),
                }
        except Exception:
            return None
        return None

    def _match_topics(self, text: str) -> List[str]:
        lowered = text.lower()
        matched = []
        for topic_key, data in TOPICS.items():
            if any(kw in lowered for kw in data["keywords"]):
                matched.append(topic_key)
        return matched

    def _format_rover_insights(self, rover_ctx: dict, lang: str) -> str:
        """Create a human-readable telemetry interpretation in the target language."""
        soil = rover_ctx.get("soil", {})
        nitrogen = soil.get("nitrogen")
        phosphorus = soil.get("phosphorus")
        potassium = soil.get("potassium")
        moisture = soil.get("moisture")
        temp = soil.get("temperature")

        lines = []
        if lang == LANG_HI:
            lines.append("📊 **वर्तमान क्रॉपसिंक रोवर आंकड़े:**")
            if moisture is not None:
                status_note = " (कम)" if moisture < 25 else " (पर्याप्त)" if moisture <= 65 else " (अधिक)"
                lines.append("• मिट्टी की नमी: %.1f%%%s" % (moisture, status_note))
            if nitrogen is not None and phosphorus is not None and potassium is not None:
                lines.append("• मिट्टी NPK: N=%.1f, P=%.1f, K=%.1f mg/kg" % (nitrogen, phosphorus, potassium))
            if temp is not None:
                lines.append("• मिट्टी का तापमान: %.1f°C" % temp)
        elif lang == LANG_TE:
            lines.append("📊 **ప్రస్తుత క్రాప్ సింక్ రోవర్ రీడింగ్స్:**")
            if moisture is not None:
                status_note = " (తక్కువ)" if moisture < 25 else " (సరిపడా)" if moisture <= 65 else " (ఎక్కువ)"
                lines.append("• నేల తేమ: %.1f%%%s" % (moisture, status_note))
            if nitrogen is not None and phosphorus is not None and potassium is not None:
                lines.append("• నేల NPK: N=%.1f, P=%.1f, K=%.1f mg/kg" % (nitrogen, phosphorus, potassium))
            if temp is not None:
                lines.append("• నేల ఉష్ణోగ్రత: %.1f°C" % temp)
        else:
            lines.append("📊 **Current CropSync Rover Telemetry:**")
            if moisture is not None:
                status_note = " (low - irrigation advised)" if moisture < 25 else " (optimal)" if moisture <= 65 else " (high)"
                lines.append("• Soil Moisture: %.1f%%%s" % (moisture, status_note))
            if nitrogen is not None and phosphorus is not None and potassium is not None:
                lines.append("• Soil NPK: N=%.1f, P=%.1f, K=%.1f mg/kg" % (nitrogen, phosphorus, potassium))
            if temp is not None:
                lines.append("• Soil Temperature: %.1f°C" % temp)

        return "\n".join(lines)

    def ask(self, question: AssistantQuestion) -> AssistantAnswer:
        """Answer an agricultural query in the requested language with contextual grounding."""
        text = (question.text or "").strip()
        lang = question.language if question.language in SUPPORTED_LANGUAGES else LANG_EN

        if not text:
            msg = "దయచేసి ఒక ప్రశ్నను అడగండి." if lang == LANG_TE else "कृपया एक कृषि संबंधित प्रश्न पूछें।" if lang == LANG_HI else "Please provide an agricultural question."
            return AssistantAnswer(text=msg, language=lang, status="error", error_code="empty_question")

        if len(text) > MAX_QUESTION_CHARS:
            text = text[:MAX_QUESTION_CHARS]

        rover_ctx = self._get_latest_rover_context() if question.include_rover_data else None
        matched_topics = self._match_topics(text)
        sources = ["CropSync Agronomic Knowledge Base V1"]

        response_paragraphs = []

        # 1. Rover Telemetry section (if present and requested)
        if rover_ctx:
            response_paragraphs.append(self._format_rover_insights(rover_ctx, lang))
            sources.append("CropSync Rover In-Field Telemetry (Sequence #%s)" % rover_ctx.get("sequence_number", 0))

        # 2. Topic-specific guidance
        if matched_topics:
            for topic in matched_topics[:2]:  # Focus on top matched topics
                topic_data = TOPICS[topic].get(lang) or TOPICS[topic][LANG_EN]
                response_paragraphs.append("🌾 **%s:**\n%s" % (topic_data["title"], topic_data["guidance"]))
        else:
            # General precision agriculture overview when query is open-ended
            if lang == LANG_HI:
                response_paragraphs.append(
                    "🌾 **फसल और मिट्टी प्रबंधन मार्गदर्शन:**\n"
                    "स्वस्थ फसल उत्पादन के लिए मिट्टी की नियमित जांच, संतुलित उर्वरक (NPK) और समय पर सिंचाई आवश्यक है। "
                    "फसल के विकास के संवेदनशील चरणों (विशेषकर फूल और दाना बनते समय) नमी की कमी न होने दें। "
                    "कीटों या बीमारियों के शुरुआती लक्षण दिखने पर तुरंत जैविक या संस्तुत उपाय करें।"
                )
            elif lang == LANG_TE:
                response_paragraphs.append(
                    "🌾 **పంట మరియు నేల యాజమాన్య సలహా:**\n"
                    "అధిక దిగుబడి సాధించడానికి నేలలో సరైన తేమ, సమతుల్య పోషకాలు (NPK) మరియు సకాలంలో సస్యరక్షణ చర్యలు అవసరం. "
                    "పూత మరియు గింజ పాలుపోసుకునే దశల్లో నీటి ఎద్దడి రాకుండా చూసుకోవడం అత్యంత ముఖ్యం. "
                    "తెగుళ్ల లేదా పురుగుల లక్షణాలు గమనించిన వెంటనే తగిన నివారణ చర్యలు చేపట్టండి."
                )
            else:
                response_paragraphs.append(
                    "🌾 **Crop & Soil Health Advisory:**\n"
                    "For optimal crop development, maintain balanced soil nutrition (NPK), monitor root-zone moisture, "
                    "and avoid moisture stress during critical reproductive windows. Regularly inspect the underside of leaves for "
                    "early symptom markers."
                )

        # 3. Contextual sensor alert if anomalies exist
        if rover_ctx:
            soil = rover_ctx.get("soil", {})
            moisture = soil.get("moisture")
            if moisture is not None and moisture < 20.0:
                if lang == LANG_HI:
                    response_paragraphs.append("⚠️ **नमी चेतावनी:** मिट्टी की नमी केवल %.1f%% है। फसल को तत्काल सिंचाई की आवश्यकता हो सकती है।" % moisture)
                elif lang == LANG_TE:
                    response_paragraphs.append("⚠️ **తేమ హెచ్చరిక:** నేల తేమ కేవలం %.1f%% మాత్రమే ఉంది. వెంటనే తడి ఇవ్వడం మంచిది." % moisture)
                else:
                    response_paragraphs.append("⚠️ **Moisture Alert:** Soil moisture is critically low at %.1f%%. Immediate irrigation is recommended." % moisture)

        final_text = "\n\n".join(response_paragraphs)
        disclaimer = ADVISORY_DISCLAIMERS.get(lang, ADVISORY_DISCLAIMERS[LANG_EN])

        return AssistantAnswer(
            text=final_text,
            language=lang,
            rover_context=rover_ctx,
            sources=tuple(sources),
            advisory_warning=disclaimer,
            status="ok"
        )
