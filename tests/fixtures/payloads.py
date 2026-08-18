"""Representative provider payloads, shaped like the real responses.

Kept as Python literals rather than JSON files so the structural quirks each
adapter has to survive (nested envelopes, string prices, split coordinates) are
visible next to the tests that assert on them.
"""

from __future__ import annotations

ZILLOW_SEARCH = {
    "props": [
        {
            "zpid": "43567890",
            "detailUrl": "/homedetails/9705-Collins-Ave-1502N/43567890_zpid/",
            "address": "9705 Collins Ave #1502N, Bal Harbour, FL 33154",
            "price": "$9,500/mo",
            "bedrooms": 2,
            "bathrooms": 2.5,
            "livingArea": 1450,
            "propertyType": "CONDO",
            "latitude": 25.8890,
            "longitude": -80.1233,
            "imgSrc": "https://photos.zillowstatic.com/fp/a.jpg",
            "listingStatus": "FOR_RENT",
        },
        {
            "zpid": "43567891",
            "detailUrl": "/homedetails/1900-Sunset-Harbour-2210/43567891_zpid/",
            "address": "1900 Sunset Harbour Dr #2210, Miami Beach, FL 33139",
            "price": "$8,000/mo",
            "bedrooms": 2,
            "bathrooms": 2,
            "livingArea": 1300,
            "propertyType": "CONDO",
            "latitude": 25.7930,
            "longitude": -80.1430,
            "imgSrc": "https://photos.zillowstatic.com/fp/b.jpg",
        },
        {
            "zpid": "43567892",
            "detailUrl": "/homedetails/too-small/43567892_zpid/",
            "address": "5959 Collins Ave #904, Miami Beach, FL 33140",
            "price": "$6,200/mo",
            "bedrooms": 1,          # dropped by the numeric pre-filter
            "bathrooms": 1,
            "livingArea": 780,
            "propertyType": "CONDO",
            "latitude": 25.8330,
            "longitude": -80.1210,
        },
    ],
    "totalPages": 1,
    "totalResultCount": 3,
}

ZILLOW_DETAIL = {
    "zpid": "43567890",
    "yearBuilt": 2018,
    "description": (
        "Direct oceanfront residence at Bal Harbour. Annual lease, 6-12 months. "
        "Fully renovated in 2021."
    ),
    "resoFacts": {
        "leaseTerm": "12 Months",
        "livingArea": "1,450 sqft",
        "associationAmenities": [
            "Valet Parking", "24-hour Concierge", "Oceanfront Pool", "Spa",
            "Fitness Center", "Beach Service",
        ],
    },
    "photos": [
        {"mixedSources": {"jpeg": [{"url": "https://photos.zillowstatic.com/fp/detail1.jpg"}]}}
    ],
}

# Realtor.com wraps everything twice and splits the address into components.
REALTOR_SEARCH = {
    "data": {
        "home_search": {
            "total": 1,
            "results": [
                {
                    "property_id": "M4567890123",
                    "permalink": "9705-Collins-Ave-Unit-1502N_Bal-Harbour_FL_33154",
                    "list_price": 9500,
                    "status": "for_rent",
                    "description": {
                        "beds": 2,
                        "baths": 2.5,
                        "sqft": 1450,
                        "type": "condos",
                        "year_built": 2018,
                        "text": (
                            "Annual lease only, 12 month term. Valet, concierge, "
                            "oceanfront pool, spa, fitness center."
                        ),
                    },
                    "location": {
                        "address": {
                            "line": "9705 Collins Ave",
                            "unit": "1502N",
                            "city": "Bal Harbour",
                            "state_code": "FL",
                            "postal_code": "33154",
                            "coordinate": {"lat": 25.8890, "lon": -80.1233},
                        }
                    },
                    "primary_photo": {"href": "https://ap.rdcpix.com/x.jpg"},
                    "tags": ["ocean_view", "valet", "concierge", "pool", "spa"],
                    "branding": [{"name": "Douglas Elliman"}],
                }
            ],
        }
    }
}

REALTYAPI_SEARCH = {
    "listings": [
        {
            "id": "RA-889",
            "listingUrl": "https://api.realtyapi.io/listing/889",
            "address": {
                "streetAddress": "9111 Collins Ave",
                "unit": "N-501",
                "city": "Surfside",
                "state": "FL",
                "postalCode": "33154",
                "latitude": 25.8790,
                "longitude": -80.1215,
            },
            "propertyType": "Condominium",
            "bedrooms": 2,
            "bathrooms": 2,
            "squareFeet": 1380,
            "yearBuilt": 2016,
            "price": 7800,
            "status": "Active",
            "PublicRemarks": (
                "Six to twelve month lease. Valet, concierge, pool, spa, gym, "
                "beach service."
            ),
            "amenities": ["Valet", "Concierge", "Pool", "Spa", "Gym"],
            "photos": [{"url": "https://cdn.realtyapi.io/889/1.jpg"}],
        }
    ],
    "total": 1,
}

# A ScrapingBee response: the portal's own state JSON embedded in the page.
SCRAPED_ZILLOW_HTML = """<!doctype html><html><head><title>Rentals</title></head><body>
<script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{"searchPageState":{"cat1":{"searchResults":{"listResults":[
 {"zpid":"991","address":"16901 Collins Ave #2202, Sunny Isles Beach, FL 33160",
  "unformattedPrice":10500,"beds":2,"baths":2.5,"area":1600,
  "latLong":{"latitude":25.9330,"longitude":-80.1206},
  "detailUrl":"/homedetails/991_zpid/","imgSrc":"https://p/991.jpg",
  "hdpData":{"homeInfo":{"livingArea":1600,"homeType":"CONDO","zipcode":"33160",
                          "city":"Sunny Isles Beach","yearBuilt":2018}}}
]}}}}}}
</script></body></html>"""

RENTCAST_RENT_AVM = {
    "rent": 8900,
    "rentRangeLow": 8300,
    "rentRangeHigh": 9500,
    "comparables": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
}

RENTCAST_VALUE_AVM = {"price": 1850000, "priceRangeLow": 1700000, "priceRangeHigh": 2000000}

HOUSECANARY_MGET = [
    {
        "address_info": {"address": "9705 Collins Ave Unit 1502N", "zipcode": "33154"},
        "property/value_rental": {
            "api_code": 0,
            "result": {"value": {"price": 9100, "price_lower": 8400, "price_upper": 9800}},
        },
        "property/value": {"api_code": 0, "result": {"value": {"price": 1920000}}},
    }
]

MIAMIDADE_ADDRESS_SEARCH = {
    "Completed": True,
    "MinimumPropertyInfos": [
        {"Folio": "12-2235-001-0720", "SiteAddress": "9705 COLLINS AVE", "Unit": "1502N"},
        {"Folio": "12-2235-001-0730", "SiteAddress": "9705 COLLINS AVE", "Unit": "1503N"},
    ],
}

MIAMIDADE_FOLIO_DETAIL = {
    "PropertyInfo": {
        "Folio": "1222350010720",
        "SiteAddress": "9705 COLLINS AVE 1502N",
        "UnitNo": "1502N",
        "BuildingHeatedArea": 1180,
        "BuildingEffectiveArea": 1420,
        "YearBuilt": 2016,
        "BedroomCount": 2,
        "BathroomCount": 2,
        "DORDescription": "RESIDENTIAL - CONDOMINIUM",
    }
}
