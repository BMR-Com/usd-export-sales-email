# BMR-Com · US Cotton Export Sales

**Weekly USDA export sales for US cotton, read against USDA's WASDE forecasts.**

🔗 **Live page:** https://bmr-com.github.io/usd-export-sales-email/

Each Thursday after USDA's 8:30 AM ET export sales release, the page updates with:

| Section | What it shows |
|---|---|
| **Brief** | Commitments and shipments vs USDA's export forecast and the usual seasonal pace, what USDA's number requires per week, this week's buyers, destinations, competing exporters, importers' own supply, and a bottom line. |
| **Seasonal pace chart** | This season's commitments against the last five seasons. |
| **Destinations** | Commitments to each major buyer vs last year and the 5-year average, next to that country's USDA import forecast and its usual share of US cotton. |
| **Competition and import need** | Brazil, Australia and the African Franc Zone (plus India in seasons when it has cotton to spare): exportable surplus and export forecasts. Own supply vs use for China, India, Pakistan, Turkey, Bangladesh, Vietnam and Indonesia. |
| **Shipment calendar** | Which months each destination usually takes its US cotton. |

Data: USDA FAS Export Sales Reporting (upland and Pima), USDA WASDE via [wasde-analytics](https://github.com/BMR-Com/wasde-analytics).

**Setup:** the data job reads the USDA FAS API key from the repository secret `FAS_API_KEY`.

The on-call and options pages and jobs in this repo are kept but switched off.

---
*BMR-Com · independent commodity research. Not investment advice.*
