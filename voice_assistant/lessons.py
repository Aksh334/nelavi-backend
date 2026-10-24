"""CropSync Agricultural Learning Lessons.
Covers 5 core precision agriculture domains in English, Hindi, and Telugu.
"""

from typing import Dict, List, Optional
from voice_assistant.boundary import LANG_EN, LANG_HI, LANG_TE, SUPPORTED_LANGUAGES

LESSONS = [
    {
        "id": "crop_care_stages",
        "category": "Crop Management",
        "duration_minutes": 5,
        "icon": "🌱",
        "content": {
            LANG_EN: {
                "title": "Crop Care & Key Growth Stages",
                "objective": "Understand the critical physiological stages of field crops and targeted care at each phase.",
                "summary": "Timely scouting and managing crop stress during vegetative and flowering transitions directly protects crop yield potential.",
                "sections": [
                    {
                        "heading": "1. Germination & Emergence",
                        "body": "Ensure adequate soil moisture without standing water. Soil compaction or crusting impedes emergence. Monitor for damping-off fungi or early cutworms."
                    },
                    {
                        "heading": "2. Vegetative Phase",
                        "body": "Rapid leaf and canopy development occurs here. Light interception and photosynthetically active radiation drive root anchoring. Nitrogen demand is active."
                    },
                    {
                        "heading": "3. Flowering & Pollination (Critical Window)",
                        "body": "The most moisture- and heat-sensitive period. Severe drought stress during pollination causes flower abortion and poor pod/grain set. Avoid pesticide sprays during peak pollinator activity hours."
                    },
                    {
                        "heading": "4. Grain/Fruit Filling to Maturity",
                        "body": "Photosynthates translocate to economic plant parts. Maintain adequate potassium for carbohydrate translocation. Taper irrigation as physiological maturity approaches."
                    }
                ],
                "farmer_tips": [
                    "Scout fields at least twice a week during vegetative and flowering transitions.",
                    "Look under leaf canopies for early signs of mites, aphids, and fungal spore patches.",
                    "Record flowering dates to accurately forecast harvest windows."
                ]
            },
            LANG_HI: {
                "title": "फसल देखभाल और विकास के मुख्य चरण",
                "objective": "फसल के विभिन्न विकास चरणों को समझना और प्रत्येक चरण में आवश्यक देखभाल करना।",
                "summary": "वानस्पतिक और फूल आने की अवस्था में फसल की निगरानी और तनाव प्रबंधन पैदावार की रक्षा करता है।",
                "sections": [
                    {
                        "heading": "1. अंकुरण और जमाव",
                        "body": "खेत में पर्याप्त नमी सुनिश्चित करें, लेकिन जलभराव न होने दें। मिट्टी की पपड़ी टूटने से अंकुरण आसानी से होता है। शुरुआती कीटों पर नजर रखें।"
                    },
                    {
                        "heading": "2. वानस्पतिक वृद्धि अवस्था",
                        "body": "इस समय पौधे तेजी से पत्ते और जड़ें फैलाते हैं। पौधे की वृद्धि के लिए नाइट्रोजन और संतुलित नमी की आवश्यकता होती है।"
                    },
                    {
                        "heading": "3. फूल आने और परागण की अवस्था (संवेदनशील समय)",
                        "body": "यह सबसे संवेदनशील चरण है। इस समय पानी की कमी से फूल झड़ सकते हैं। परागण के समय कीटनाशक छिड़काव से बचें ताकि मित्र कीट सुरक्षित रहें।"
                    },
                    {
                        "heading": "4. दाना/फल भराव और परिपक्वता",
                        "body": "इस अवस्था में पोटाश की पर्याप्त मात्रा दानों को मजबूत बनाती है। कटाई से पहले सिंचाई धीरे-धीरे कम करें।"
                    }
                ],
                "farmer_tips": [
                    "सप्ताह में कम से कम दो बार अपने खेत का निरीक्षण करें।",
                    "पत्तियों के निचले हिस्से में फफूंद या रस चूसने वाले कीटों की जांच करें।",
                    "फूल आने की सही तारीख लिखकर रखें ताकि कटाई का सही अनुमान लगाया जा सके।"
                ]
            },
            LANG_TE: {
                "title": "పంట సంరక్షణ & ప్రధాన ఎదుగుదల దశలు",
                "objective": "పంట ఎదుగుదల దశలను అర్థం చేసుకుని ప్రతి దశలో తగిన రక్షణ చర్యలు తీసుకోవడం.",
                "summary": "మొక్కల ఎదుగుదల మరియు పూత దశల్లో ఒత్తిడిని నివారించడం ద్వారా అధిక దిగుబడిని పొందవచ్చు.",
                "sections": [
                    {
                        "heading": "1. మొలకెత్తే దశ",
                        "body": "విత్తనాలు సరిగ్గా మొలకెత్తడానికి తగినంత తేమ అవసరం. నీరు నిలబడకుండా చూసుకోవాలి. భూమిపై గట్టి పొర ఏర్పడకుండా జాగ్రత్త పడాలి."
                    },
                    {
                        "heading": "2. శాకీయ (ఎదుగుదల) దశ",
                        "body": "ఆకులు, కొమ్మలు మరియు వేర్లు వేగంగా వృద్ధి చెందే సమయం. ఈ దశలో సమతుల్య పోషకాలు మరియు తేమ చాలా ముఖ్యం."
                    },
                    {
                        "heading": "3. పూత మరియు పిందె దశ (కీలక సమయం)",
                        "body": "ఇది పంట జీవితంలో అత్యంత సున్నితమైన దశ. ఈ సమయంలో నీటి ఎద్దడి రాకుండా చూడాలి. పరాగసంపర్క కీటకాలకు హాని కలగకుండా మందుల పిచికారీ జాగ్రత్తగా చేయాలి."
                    },
                    {
                        "heading": "4. గింజ/కాయ ముదిరే దశ",
                        "body": "గింజలు గట్టిపడేందుకు పొటాష్ పోషకం దోహదపడుతుంది. పంట కోతకు వస్తున్నప్పుడు నీటి తడులను తగ్గించాలి."
                    }
                ],
                "farmer_tips": [
                    "వారానికి కనీసం రెండు సార్లు చేనును గమనించండి.",
                    "ఆకుల అడుగు భాగాన రసం పీల్చే పురుగులు లేదా మచ్చల తెగులు ఉందేమో పరిశీలించండి.",
                    "పూత వచ్చిన సమయాన్ని బట్టి కోత సమయాన్ని సులభంగా అంచనా వేయవచ్చు."
                ]
            }
        }
    },
    {
        "id": "soil_health_testing",
        "category": "Soil & Nutrients",
        "duration_minutes": 6,
        "icon": "🧪",
        "content": {
            LANG_EN: {
                "title": "Soil Health, Sensor Testing & NPK Balance",
                "objective": "Learn how sensor probes measure soil parameters (N-P-K, moisture, temperature) and how to interpret readings.",
                "summary": "Regular probe testing reveals localized nutrient depletion and prevents over-fertilization.",
                "sections": [
                    {
                        "heading": "1. The Primary Nutrients (NPK)",
                        "body": "Nitrogen (N) builds chlorophyll and vegetative vigour. Phosphorus (P) accelerates root development and early energy transfer. Potassium (K) regulates stomatal conductance, water retention, and disease resistance."
                    },
                    {
                        "heading": "2. Sensor Probe Telemetry",
                        "body": "CropSync's rover probe measures electrical conductivity and soil matrix potential. Volumetric moisture reflects available water in the root zone. Calibrated sensor status flags (NPK, moisture, temp) ensure reading reliability."
                    },
                    {
                        "heading": "3. Soil Temperature & Microbial Activity",
                        "body": "Biological nitrification peaks between 20°C and 30°C. Cold soil (<15°C) slows phosphorus availability regardless of total soil reserve."
                    }
                ],
                "farmer_tips": [
                    "Sample soil at consistent probe insertion depths (15–20 cm) for root zone accuracy.",
                    "High fertilizer salt concentrations can skew conductivity; verify moisture alongside NPK.",
                    "Perform laboratory soil health card tests every 2–3 seasons to baseline organic carbon."
                ]
            },
            LANG_HI: {
                "title": "मिट्टी का स्वास्थ्य, सेंसर परीक्षण और NPK संतुलन",
                "objective": "सेंसर जांच के माध्यम से मिट्टी के पोषक तत्वों (N-P-K, नमी, तापमान) को समझना।",
                "summary": "नियमित मिट्टी परीक्षण से पोषक तत्वों की सही जानकारी मिलती है और अनावश्यक खाद की बर्बादी रुकती है।",
                "sections": [
                    {
                        "heading": "1. मुख्य पोषक तत्व (NPK)",
                        "body": "नाइट्रोजन (N) पत्तियों की हरियाली और बढ़वार के लिए है। फास्फोरस (P) जड़ों के फैलाव और फूलों के लिए आवश्यक है। पोटाश (K) पौधों को रोगों से लड़ने और सूखे को सहने की ताकत देता है।"
                    },
                    {
                        "heading": "2. रोवर सेंसर द्वारा मिट्टी जांच",
                        "body": "क्रॉपसिंक रोवर का प्रोब मिट्टी की नमी और चालकता को मापता है। इससे जड़ों के पास उपलब्ध नमी और पोषक तत्वों का सही स्तर पता चलता है।"
                    },
                    {
                        "heading": "3. मिट्टी का तापमान और सूक्ष्मजीव",
                        "body": "20°C से 30°C के बीच मिट्टी के लाभकारी जीवाणु सबसे सक्रिय रहते हैं। बहुत ठंडी मिट्टी में पौधों को फास्फोरस ग्रहण करने में कठिनाई होती है।"
                    }
                ],
                "farmer_tips": [
                    "सेंसर प्रोब को हमेशा 15-20 सेमी की गहराई तक लगाएं ताकि जड़ों का स्तर जांचा जा सके।",
                    "खेत में खाद डालने के तुरंत बाद जांच न करें; खाद घुलने के बाद सही आंकड़े मिलते हैं।",
                    "मिट्टी की जैविक गुणवत्ता बनाए रखने के लिए गोबर की खाद या हरी खाद का प्रयोग करें।"
                ]
            },
            LANG_TE: {
                "title": "నేల ఆరోగ్యం, సెన్సార్ పరీక్ష & NPK సమతుల్యత",
                "objective": "సెన్సార్ ప్రోబ్ ద్వారా నేలలోని పోషకాలు (N-P-K), తేమ మరియు ఉష్ణోగ్రతను తెలుసుకోవడం.",
                "summary": "నేల పరీక్ష ద్వారా పోషకాల లోపాలను ముందుగానే గుర్తించి సరైన ఎరువులను మాత్రమే వాడవచ్చు.",
                "sections": [
                    {
                        "heading": "1. ప్రధాన పోషకాలు (NPK)",
                        "body": "నత్రజని (N) మొక్క పచ్చదనానికి, ఎదుగుదలకు తోడ్పడుతుంది. భాస్వరం (P) వేళ్ల వృద్ధికి, పూతకు ముఖ్యం. పొటాషియం (K) రోగనిరోధక శక్తిని, కాయ నాణ్యతను పెంచుతుంది."
                    },
                    {
                        "heading": "2. రోవర్ సెన్సార్ రీడింగ్స్",
                        "body": "క్రాప్ సింక్ రోవర్ ప్రోబ్ నేలలోని తేమ శాతం మరియు ఉష్ణోగ్రతను కచ్చితంగా కొలుస్తుంది. వేర్లు ఉండే లోతులోని తేమను ఇది సూచిస్తుంది."
                    },
                    {
                        "heading": "3. నేల ఉష్ణోగ్రత మరియు సూక్ష్మజీవులు",
                        "body": "నేల ఉష్ణోగ్రత 20°C నుండి 30°C మధ్య ఉన్నప్పుడు నేలలోని మేలు చేసే బ్యాక్టీరియా చురుగ్గా పనిచేసి పోషకాలను మొక్కలకు అందిస్తుంది."
                    }
                ],
                "farmer_tips": [
                    "ప్రోబ్‌ను నేలలో 15-20 సెం.మీ లోతు వరకు దింపి రీడింగ్ తీసుకోవాలి.",
                    "ఎరువులు వేసిన వెంటనే కాకుండా తడి ఆరిన తర్వాత పరీక్షించాలి.",
                    "పచ్చిరొట్ట ఎరువులు వాడటం ద్వారా నేల సారాన్ని సహజంగా కాపాడుకోవచ్చు."
                ]
            }
        }
    },
    {
        "id": "irrigation_water_mgmt",
        "category": "Water Management",
        "duration_minutes": 5,
        "icon": "💧",
        "content": {
            LANG_EN: {
                "title": "Irrigation Efficiency & Soil Water Dynamics",
                "objective": "Optimize irrigation timing using sensor-guided moisture thresholds and water conservation methods.",
                "summary": "Watering based on sensor moisture rather than fixed calendar intervals prevents root asphyxiation and leaching.",
                "sections": [
                    {
                        "heading": "1. Field Capacity vs Wilting Point",
                        "body": "Field capacity is maximum water retained after gravity drainage. Permanent wilting point is when plants can no longer extract water. Manage irrigation within the readily available water buffer (50–70% field capacity)."
                    },
                    {
                        "heading": "2. Drip & Micro-Irrigation Benefits",
                        "body": "Drip delivers water straight to root zones, reducing evaporation loss by up to 40% compared to flood irrigation. It minimizes weed germination between crop rows."
                    },
                    {
                        "heading": "3. Drainage & Soil Aeration",
                        "body": "Waterlogged soil strips oxygen from root zones within 24–48 hours, inducing anaerobic root decay and yellowing resembling nitrogen deficiency."
                    }
                ],
                "farmer_tips": [
                    "Irrigate in early morning or late afternoon to cut evaporative losses.",
                    "Use mulch layers to retain soil moisture and stabilize soil temperatures.",
                    "Watch rover moisture telemetry before initiating scheduled pump runs."
                ]
            },
            LANG_HI: {
                "title": "सिंचाई प्रबंधन और जल संरक्षण",
                "objective": "सेंसर आधारित नमी माप से सही समय पर सिंचाई करना और पानी बचाना।",
                "summary": "कैलेंडर के बजाय मिट्टी की वास्तविक नमी देखकर सिंचाई करने से फसल की जड़ें स्वस्थ रहती हैं।",
                "sections": [
                    {
                        "heading": "1. मिट्टी में नमी का स्तर",
                        "body": "खेत में नमी इतनी होनी चाहिए कि मिट्टी का लड्डू बन सके लेकिन हाथ में पानी न चिपके। अत्यधिक पानी से जड़ों को हवा नहीं मिलती।"
                    },
                    {
                        "heading": "2. टपक (ड्रिप) सिंचाई के लाभ",
                        "body": "ड्रिप सिंचाई से पानी सीधे जड़ों तक पहुंचता है। इससे 40% तक पानी की बचत होती है और क्यारियों में खरपतवार कम उगते हैं।"
                    },
                    {
                        "heading": "3. जल निकासी का महत्व",
                        "body": "खेत में 24-48 घंटे से अधिक पानी खड़ा रहने पर जड़ें सड़ने लगती हैं और पत्तियां पीली पड़ने लगती हैं। जल निकासी की व्यवस्था जरूरी है।"
                    }
                ],
                "farmer_tips": [
                    "सुबह जल्दी या शाम के समय सिंचाई करें ताकि वाष्पीकरण कम हो।",
                    "फसल के अवशेषों या मल्चिंग का उपयोग करके नमी को अधिक समय तक बचाएं।",
                    "सिंचाई से पहले रोवर के नमी सेंसर के आंकड़े अवश्य देखें।"
                ]
            },
            LANG_TE: {
                "title": "నీటి యాజమాన్యం & సమర్థవంతమైన సాగునీరు",
                "objective": "నేలలో తేమ శాతాన్ని బట్టి సరైన సమయంలో నీరు పెట్టడం మరియు నీటిని ఆదా చేయడం.",
                "summary": "రోజుల లెక్కన కాకుండా నేలలోని తేమను బట్టి తడులు ఇవ్వడం వల్ల పంట వేర్లు దృఢంగా ఉంటాయి.",
                "sections": [
                    {
                        "heading": "1. నేలలో తేమ స్థాయిలు",
                        "body": "మట్టిని చేత్తో పట్టుకుంటే ముద్దలా అవ్వాలి కానీ నీరు కారకూడదు. ఎక్కువ నీరు నిల్వ ఉంటే వేర్లకు ఆక్సిజన్ అందదు."
                    },
                    {
                        "heading": "2. బిందు (డ్రిప్) సేద్యం ప్రయోజనాలు",
                        "body": "డ్రిప్ విధానం ద్వారా నీరు నేరుగా వేర్లకే అందుతుంది. 40% వరకు నీరు ఆదా అవుతుంది మరియు కలుపు మొక్కల సమస్య తగ్గుతుంది."
                    },
                    {
                        "heading": "3. మురుగు నీటి పారుదల",
                        "body": "చేనులో 24 గంటల కంటే ఎక్కువ సమయం నీరు నిలిచి ఉంటే వేరుకుళ్లు తెగులు వస్తుంది. ఆకులు పసుపు రంగులోకి మారతాయి."
                    }
                ],
                "farmer_tips": [
                    "ఉదయం లేదా సాయంత్రం వేళల్లో నీటి తడులు ఇవ్వడం మంచిది.",
                    "మల్చింగ్ వాడటం వల్ల నేలలో తేమ ఎక్కువ రోజులు నిల్వ ఉంటుంది.",
                    "బోరు మోటారు వేసే ముందు రోవర్ సెన్సార్ తేమ రీడింగ్ చూడండి."
                ]
            }
        }
    },
    {
        "id": "nutrient_fertilizer_mgmt",
        "category": "Nutrient Management",
        "duration_minutes": 6,
        "icon": "🌿",
        "content": {
            LANG_EN: {
                "title": "Nutrient Management & Balanced Fertilization",
                "objective": "Understand targeted nutrient application, organic integration, and preventing nutrient run-off.",
                "summary": "Split fertilizer applications matched to crop uptake curves yield higher recovery efficiency than single basal dumping.",
                "sections": [
                    {
                        "heading": "1. Split Application Strategy",
                        "body": "Applying nitrogen in splits (e.g. basal, active tillering/branching, panicle/flowering initiation) minimizes volatilization and nitrate leaching into groundwater."
                    },
                    {
                        "heading": "2. Micronutrients Matter",
                        "body": "Zinc, Iron, Boron, and Manganese are required in grams per hectare, yet their deficiency causes severe chlorosis, rosette formation, and aborted flower sets."
                    },
                    {
                        "heading": "3. Organic & Bio-Fertilizer Synergy",
                        "body": "Compost and biofertilizers (Azotobacter, Rhizobium, PSB) improve soil cation exchange capacity and solubilize locked native soil minerals."
                    }
                ],
                "farmer_tips": [
                    "Never broadcast urea on completely dry or heavily waterlogged soils.",
                    "Correct micronutrient deficiencies via targeted foliar sprays during cloudy mornings.",
                    "Always consult your local agricultural officer for crop-specific dosage rates."
                ]
            },
            LANG_HI: {
                "title": "पोषक तत्व प्रबंधन और संतुलित खाद",
                "objective": "फसल की जरूरत के अनुसार संतुलित उर्वरक देना और रासायनिक खाद की दक्षता बढ़ाना।",
                "summary": "एक साथ पूरी खाद डालने के बजाय किश्तों में खाद देने से फसल को पूरा पोषण मिलता है।",
                "sections": [
                    {
                        "heading": "1. किश्तों में खाद देना",
                        "body": "यूरिया जैसे नाइट्रोजन वाले उर्वरकों को 2-3 बार में बांटकर देने से खाद बर्बाद नहीं होती और पौधे तेजी से बढ़ते हैं।"
                    },
                    {
                        "heading": "2. सूक्ष्म पोषक तत्वों का महत्व",
                        "body": "जिंक, बोरॉन और आयरन की थोड़ी सी भी कमी से पत्तियां पीली हो जाती हैं और फल-फूल कमजोर हो जाते हैं।"
                    },
                    {
                        "heading": "3. जैविक खाद का समन्वय",
                        "body": "गोबर की खाद और केंचुआ खाद मिट्टी में कार्बन बढ़ाती हैं जिससे रासायनिक खादों की कार्यक्षमता भी बढ़ जाती है।"
                    }
                ],
                "farmer_tips": [
                    "बिल्कुल सूखी जमीन या भरे हुए पानी में यूरिया का छिड़काव न करें।",
                    "जिंक या बोरॉन की कमी दिखने पर पत्तों पर स्प्रे करना अधिक असरदार होता है।",
                    "खाद की सही मात्रा के लिए स्थानीय कृषि विस्तार अधिकारी से सलाह लें।"
                ]
            },
            LANG_TE: {
                "title": "పోషకాల యాజమాన్యం & సమతుల్య ఎరువులు",
                "objective": "పంటకు అవసరమైన మోతాదులో మాత్రమే సమతుల్య ఎరువులను వాడటం మరియు నేల సారాన్ని పెంచడం.",
                "summary": "ఎరువులను ఒకేసారి కాకుండా దఫాలుగా వేయడం వల్ల పంటకు ఎక్కువ ప్రయోజనం కలుగుతుంది.",
                "sections": [
                    {
                        "heading": "1. దఫాలుగా ఎరువులు వేయడం",
                        "body": "యూరియా లాంటి నత్రజని ఎరువులను రెండు లేదా మూడు దఫాలుగా వేస్తే మొక్కలకు సరిగ్గా అందుతుంది మరియు వృథా కాదు."
                    },
                    {
                        "heading": "2. సూక్ష్మ పోషకాల ఆవశ్యకత",
                        "body": "జింక్, బోరాన్, ఐరన్ లాంటి సూక్ష్మ పోషకాలు కొద్ది మోతాదులో అవసరమైనప్పటికీ, ఇవి లేకపోతే పూత రాలడం, ఆకులు పసుపుబారడం జరుగుతుంది."
                    },
                    {
                        "heading": "3. సేంద్రియ ఎరువుల ప్రాముఖ్యత",
                        "body": "పశువుల ఎరువు, వర్మీ కంపోస్ట్ వాడటం వల్ల నేల గుల్లబారి రసాయన ఎరువులను పీల్చుకునే శక్తి పెరుగుతుంది."
                    }
                ],
                "farmer_tips": [
                    "పూర్తిగా ఎండిన నేలలో లేదా నీరు నిల్వ ఉన్నప్పుడు యూరియా వేయకండి.",
                    "సూక్ష్మ పోషకాల లోపం ఉన్నప్పుడు పిచికారీ చేయడం వల్ల త్వరితగతిన ఫలితం ఉంటుంది.",
                    "ఎరువుల సిఫార్సు మోతాదుల కోసం వ్యవసాయ అధికారి సలహా పాటించండి."
                ]
            }
        }
    },
    {
        "id": "rover_telemetry_sensing",
        "category": "CropSync Technology",
        "duration_minutes": 5,
        "icon": "🤖",
        "content": {
            LANG_EN: {
                "title": "CropSync Rover Telemetry & In-Field Sensing",
                "objective": "Learn how the CropSync autonomous rover measures soil and leaf telemetry to generate field records.",
                "summary": "Integrating rover soil probes with visual leaf inspection provides holistic in-field diagnostics.",
                "sections": [
                    {
                        "heading": "1. Rover Mission & Sequence",
                        "body": "The rover drives along crop rows, stops at designated waypoints, lowers the sensor probe into the soil, records telemetry, and transmits validated Records."
                    },
                    {
                        "heading": "2. Multi-Sensor Data Fusion",
                        "body": "A single Record captures soil parameters (NPK, moisture, temperature), ambient air conditions (temperature, humidity, pressure), and rover battery voltage."
                    },
                    {
                        "heading": "3. Vision Analysis & Leaf Observations",
                        "body": "High-resolution camera snapshots capture canopy symptoms, leaf spot patterns, and growth stage evidence without destructive plant sampling."
                    }
                ],
                "farmer_tips": [
                    "Verify battery voltage before dispatching the rover on full-field scanning missions.",
                    "Ensure probe tip is clean and free of caked clay residues for consistent readings.",
                    "Use rover telemetry to identify spatial micro-zones requiring custom irrigation."
                ]
            },
            LANG_HI: {
                "title": "क्रॉपसिंक रोवर तकनीक और फील्ड सेंसिंग",
                "objective": "क्रॉपसिंक रोवर द्वारा मिट्टी और पत्तियों के आंकड़ों के संकलन को समझना।",
                "summary": "रोवर के सेंसर और कैमरे मिलकर खेत की स्थिति की सटीक रिपोर्ट तैयार करते हैं।",
                "sections": [
                    {
                        "heading": "1. रोवर का कार्य",
                        "body": "रोवर खेत में कतारों के बीच चलता है, निश्चित स्थानों पर रुककर मिट्टी में प्रोब डालता है और डेटा रिकॉर्ड करता है।"
                    },
                    {
                        "heading": "2. कई सेंसरों का एक साथ काम",
                        "body": "एक ही रिकॉर्ड में मिट्टी के पोषक तत्व, नमी, तापमान के साथ-साथ हवा का तापमान और आर्द्रता भी दर्ज होती है।"
                    },
                    {
                        "heading": "3. पत्तियों की कैमरा जांच",
                        "body": "रोवर का उच्च-क्षमता वाला कैमरा पत्तियों के रंग और धब्बों को देखकर शुरुआती लक्षणों की पहचान में मदद करता है।"
                    }
                ],
                "farmer_tips": [
                    "रोवर को खेत में भेजने से पहले उसकी बैटरी जांच अवश्य कर लें।",
                    "सेंसर प्रोब पर मिट्टी की सूखी परत न जमने दें, उसे साफ रखें।",
                    "खेत के अलग-अलग हिस्सों के आंकड़ों को देखकर ही सिंचाई और खाद की योजना बनाएं।"
                ]
            },
            LANG_TE: {
                "title": "క్రాప్ సింక్ రోవర్ టెక్నాలజీ & సెన్సింగ్",
                "objective": "క్రాప్ సింక్ రోవర్ పొలంలో నేల మరియు ఆకుల సమాచారాన్ని ఎలా నమోదు చేస్తుందో తెలుసుకోవడం.",
                "summary": "రోవర్ సెన్సార్లు మరియు కెమెరా కలిసి పొలం యొక్క సమగ్ర నివేదికను అందిస్తాయి.",
                "sections": [
                    {
                        "heading": "1. రోవర్ పనితీరు",
                        "body": "రోవర్ పంట సాళ్ళ మధ్య తిరుగుతూ, నిర్దేశిత స్థానాల్లో ఆగి సెన్సార్ ప్రోబ్‌ను నేలలోకి దింపి రీడింగ్స్ సేకరిస్తుంది."
                    },
                    {
                        "heading": "2. మల్టీ-సెన్సార్ డాటా",
                        "body": "ఒకే రికార్డులో నేల పోషకాలు, తేమ, ఉష్ణోగ్రత మరియు గాలిలోని తేమ శాతం లాంటి వివరాలు నమోదవుతాయి."
                    },
                    {
                        "heading": "3. కెమెరా పరిశీలన",
                        "body": "రోవర్ కెమెరా ఆకుల రంగు, మచ్చలను ఫోటో తీసి పంట ఎదుగుదలను మరియు తెగుళ్ల లక్షణాలను విశ్లేషిస్తుంది."
                    }
                ],
                "farmer_tips": [
                    "రోవర్‌ను పొలంలోకి పంపే ముందు బ్యాటరీ చార్జింగ్ పరిశీలించండి.",
                    "సెన్సార్ కొన భాగంలో మట్టి పేరుకుపోకుండా శుభ్రంగా ఉంచాలి.",
                    "పొలంలో ఏయే భాగాల్లో తేమ తక్కువగా ఉందో రోవర్ మ్యాప్ ద్వారా తెలుసుకోవచ్చు."
                ]
            }
        }
    }
]


def get_lessons_catalog(language: str = LANG_EN) -> List[Dict]:
    """Return lightweight lesson summaries for list views."""
    lang = language if language in SUPPORTED_LANGUAGES else LANG_EN
    catalog = []
    for lesson in LESSONS:
        content = lesson["content"].get(lang) or lesson["content"][LANG_EN]
        catalog.append({
            "id": lesson["id"],
            "category": lesson["category"],
            "duration_minutes": lesson["duration_minutes"],
            "icon": lesson["icon"],
            "title": content["title"],
            "objective": content["objective"],
            "summary": content["summary"],
        })
    return catalog


def get_lesson_detail(lesson_id: str, language: str = LANG_EN) -> Optional[Dict]:
    """Return complete lesson details in the chosen language."""
    lang = language if language in SUPPORTED_LANGUAGES else LANG_EN
    for lesson in LESSONS:
        if lesson["id"] == lesson_id:
            content = lesson["content"].get(lang) or lesson["content"][LANG_EN]
            return {
                "id": lesson["id"],
                "category": lesson["category"],
                "duration_minutes": lesson["duration_minutes"],
                "icon": lesson["icon"],
                "language": lang,
                "title": content["title"],
                "objective": content["objective"],
                "summary": content["summary"],
                "sections": content["sections"],
                "farmer_tips": content["farmer_tips"],
            }
    return None
