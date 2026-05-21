-- Home Pricing Model — MySQL Schema
-- Database: home_pricing
-- Cities: Berkeley, Orinda, Moraga, Lafayette, Walnut Creek, Alamo, Danville, San Ramon (CA)

CREATE DATABASE IF NOT EXISTS home_pricing CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE home_pricing;

-- ─── PROPERTIES ───────────────────────────────────────────────────────────────
-- Master record for each unique parcel/address
CREATE TABLE IF NOT EXISTS properties (
    id                  INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    address             VARCHAR(255) NOT NULL,
    city                VARCHAR(64) NOT NULL,
    state               CHAR(2) NOT NULL DEFAULT 'CA',
    zip_code            CHAR(5) NOT NULL,
    county              VARCHAR(64) DEFAULT 'Contra Costa',
    latitude            DECIMAL(9,6),
    longitude           DECIMAL(9,6),
    apn                 VARCHAR(32),           -- Assessor Parcel Number
    bedrooms            TINYINT UNSIGNED,
    bathrooms           DECIMAL(4,1),
    sqft_living         SMALLINT UNSIGNED,
    sqft_lot            INT UNSIGNED,
    year_built          SMALLINT UNSIGNED,
    stories             TINYINT UNSIGNED,
    garage_spaces       TINYINT UNSIGNED,
    pool                BOOLEAN DEFAULT FALSE,
    -- Penalty/adjustment flags (set by feature enrichment)
    busy_street         BOOLEAN DEFAULT FALSE,  -- arterial/highway adjacency
    slope_grade         TINYINT UNSIGNED,        -- 0–100 slope severity score
    corner_lot          BOOLEAN DEFAULT FALSE,
    view_score          TINYINT UNSIGNED DEFAULT 0, -- 0 none, 1 partial, 2 panoramic
    hoa_monthly         SMALLINT UNSIGNED DEFAULT 0,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_address (address, zip_code),
    INDEX idx_city (city),
    INDEX idx_zip (zip_code),
    INDEX idx_coords (latitude, longitude)
) ENGINE=InnoDB;

-- ─── LISTINGS ─────────────────────────────────────────────────────────────────
-- Each time a property hits the market (active, pending, withdrawn, expired)
CREATE TABLE IF NOT EXISTS listings (
    id                  INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    property_id         INT UNSIGNED NOT NULL,
    source              ENUM('zillow','redfin','realtor','county','manual') NOT NULL,
    source_id           VARCHAR(64),            -- external listing ID
    list_date           DATE NOT NULL,
    list_price          INT UNSIGNED NOT NULL,
    status              ENUM('active','pending','sold','withdrawn','expired') NOT NULL DEFAULT 'active',
    price_per_sqft      DECIMAL(8,2),
    days_on_market      SMALLINT UNSIGNED DEFAULT 0,
    price_reductions    TINYINT UNSIGNED DEFAULT 0,
    last_price_cut_date DATE,
    last_price_cut_amt  INT,                   -- negative = reduction
    description_text    TEXT,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (property_id) REFERENCES properties(id) ON DELETE CASCADE,
    INDEX idx_property (property_id),
    INDEX idx_status (status),
    INDEX idx_list_date (list_date),
    UNIQUE KEY uq_source_listing (source, source_id)
) ENGINE=InnoDB;

-- ─── SALES ────────────────────────────────────────────────────────────────────
-- Closed/recorded transactions — ground truth for model training
CREATE TABLE IF NOT EXISTS sales (
    id                  INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    property_id         INT UNSIGNED NOT NULL,
    listing_id          INT UNSIGNED,           -- link back to listing if known
    source              ENUM('zillow','redfin','county','manual') NOT NULL,
    sale_date           DATE NOT NULL,
    sale_price          INT UNSIGNED NOT NULL,
    list_price          INT UNSIGNED,
    list_to_sale_ratio  DECIMAL(6,4),           -- sale_price / list_price
    days_on_market      SMALLINT UNSIGNED,
    cash_purchase       BOOLEAN DEFAULT FALSE,
    FOREIGN KEY (property_id) REFERENCES properties(id) ON DELETE CASCADE,
    FOREIGN KEY (listing_id) REFERENCES listings(id) ON DELETE SET NULL,
    INDEX idx_property (property_id),
    INDEX idx_sale_date (sale_date),
    INDEX idx_city_date (sale_date)
) ENGINE=InnoDB;

-- ─── MARKET METRICS ───────────────────────────────────────────────────────────
-- Aggregated weekly snapshot per city/zip — feeds hedonic controls
CREATE TABLE IF NOT EXISTS market_metrics (
    id                  INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    metric_date         DATE NOT NULL,
    city                VARCHAR(64),
    zip_code            CHAR(5),
    granularity         ENUM('city','zip') NOT NULL DEFAULT 'zip',
    active_listings     SMALLINT UNSIGNED,
    median_list_price   INT UNSIGNED,
    median_sale_price   INT UNSIGNED,
    median_dom          SMALLINT UNSIGNED,      -- median days on market
    absorption_rate     DECIMAL(5,2),           -- sales per month / active listings
    list_to_sale_ratio  DECIMAL(6,4),
    price_cut_pct       DECIMAL(5,2),           -- % of listings with a price cut
    new_listings        SMALLINT UNSIGNED,
    sold_count          SMALLINT UNSIGNED,
    source              VARCHAR(32),
    INDEX idx_date_city (metric_date, city),
    INDEX idx_date_zip (metric_date, zip_code),
    UNIQUE KEY uq_metric (metric_date, granularity, city, zip_code, source)
) ENGINE=InnoDB;

-- ─── ZHVI — ZILLOW HOME VALUE INDEX ───────────────────────────────────────────
-- Zillow Research zip-level time series (monthly)
CREATE TABLE IF NOT EXISTS zhvi (
    id                  INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    zip_code            CHAR(5) NOT NULL,
    city                VARCHAR(64),
    metric_date         DATE NOT NULL,          -- first of month
    home_value          INT UNSIGNED,           -- ZHVI in dollars
    source              VARCHAR(32) DEFAULT 'zillow_research',
    UNIQUE KEY uq_zhvi (zip_code, metric_date),
    INDEX idx_zip_date (zip_code, metric_date)
) ENGINE=InnoDB;

-- ─── ECONOMIC INDICATORS ──────────────────────────────────────────────────────
-- FRED time-series data (national + Bay Area)
CREATE TABLE IF NOT EXISTS economic_indicators (
    id                  INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    indicator_date      DATE NOT NULL,
    series_id           VARCHAR(32) NOT NULL,   -- FRED series ID e.g. MORTGAGE30US
    series_name         VARCHAR(128),
    value               DECIMAL(12,4),
    unit                VARCHAR(32),
    frequency           ENUM('daily','weekly','monthly','quarterly') NOT NULL,
    source              VARCHAR(32) DEFAULT 'FRED',
    UNIQUE KEY uq_series_date (series_id, indicator_date),
    INDEX idx_series (series_id),
    INDEX idx_date (indicator_date)
) ENGINE=InnoDB;

-- ─── CONSUMER SENTIMENT ───────────────────────────────────────────────────────
-- Google Trends search volume + Redfin demand signals
CREATE TABLE IF NOT EXISTS consumer_sentiment (
    id                  INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    metric_date         DATE NOT NULL,
    city                VARCHAR(64),
    zip_code            CHAR(5),
    granularity         ENUM('city','zip','metro') NOT NULL,
    source              ENUM('google_trends','redfin_demand','zillow_survey') NOT NULL,
    metric_name         VARCHAR(64) NOT NULL,   -- e.g. 'search_volume_homes_for_sale'
    value               DECIMAL(10,4),
    UNIQUE KEY uq_sentiment (metric_date, granularity, city, zip_code, source, metric_name),
    INDEX idx_date_city (metric_date, city)
) ENGINE=InnoDB;

-- ─── MODEL PREDICTIONS ────────────────────────────────────────────────────────
-- Output of each model run for a property
CREATE TABLE IF NOT EXISTS model_predictions (
    id                  INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    property_id         INT UNSIGNED NOT NULL,
    run_date            DATE NOT NULL,
    model_version       VARCHAR(32) NOT NULL,
    hedonic_estimate    INT UNSIGNED,           -- hedonic regression output
    ml_estimate         INT UNSIGNED,           -- XGBoost/LightGBM output
    ensemble_estimate   INT UNSIGNED,           -- weighted blend
    confidence_low      INT UNSIGNED,
    confidence_high     INT UNSIGNED,
    suggested_offer     INT UNSIGNED,           -- final recommendation
    offer_strategy      ENUM('aggressive','market','conservative') DEFAULT 'market',
    feature_json        JSON,                   -- SHAP values / feature contributions
    notes               TEXT,
    FOREIGN KEY (property_id) REFERENCES properties(id) ON DELETE CASCADE,
    INDEX idx_property_date (property_id, run_date),
    INDEX idx_run_date (run_date)
) ENGINE=InnoDB;

-- ─── PROPERTY FLAGS ───────────────────────────────────────────────────────────
-- Daily-refresh generated alerts
CREATE TABLE IF NOT EXISTS property_flags (
    id                  INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    property_id         INT UNSIGNED NOT NULL,
    listing_id          INT UNSIGNED,
    flag_date           DATE NOT NULL,
    flag_type           ENUM(
                            'stale_listing',        -- DOM > city median * 1.5
                            'price_reduction',      -- any price cut detected
                            'multiple_reductions',  -- 2+ cuts
                            'below_estimate',       -- list price < model estimate
                            'above_estimate',       -- list price > model estimate by >10%
                            'new_listing',          -- first seen today
                            'back_on_market',       -- re-listed after pending/sold
                            'hot_market_alert'      -- absorption rate spike in zip
                        ) NOT NULL,
    flag_detail         VARCHAR(512),
    is_active           BOOLEAN DEFAULT TRUE,
    resolved_at         DATE,
    FOREIGN KEY (property_id) REFERENCES properties(id) ON DELETE CASCADE,
    FOREIGN KEY (listing_id) REFERENCES listings(id) ON DELETE SET NULL,
    INDEX idx_property (property_id),
    INDEX idx_flag_date (flag_date),
    INDEX idx_flag_type (flag_type),
    INDEX idx_active (is_active)
) ENGINE=InnoDB;

-- ─── REFERENCE: TARGET CITIES ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS target_cities (
    id          TINYINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    city        VARCHAR(64) NOT NULL UNIQUE,
    county      VARCHAR(64) NOT NULL DEFAULT 'Contra Costa',
    zip_codes   VARCHAR(128),                   -- comma-separated
    is_active   BOOLEAN DEFAULT TRUE
) ENGINE=InnoDB;

INSERT IGNORE INTO target_cities (city, county, zip_codes) VALUES
    ('Berkeley',      'Alameda',      '94702,94703,94704,94705,94706,94707,94708,94709,94710'),
    ('Orinda',        'Contra Costa', '94563'),
    ('Moraga',        'Contra Costa', '94556'),
    ('Lafayette',     'Contra Costa', '94549'),
    ('Walnut Creek',  'Contra Costa', '94595,94596,94597,94598'),
    ('Alamo',         'Contra Costa', '94507'),
    ('Danville',      'Contra Costa', '94506,94526'),
    ('San Ramon',     'Contra Costa', '94582,94583');
