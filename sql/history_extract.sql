-- =====================================================================
-- MDS CAB RECOMMENDER — history extract
-- One row per trip. Hyderabad BUs. 2026-05-01 .. 2026-08-01 (3 months).
--
-- Joins:
--   fact_trip                     trip + cab + vendor + timings
--   subvendor_trips_mapping       the SV the deployer actually picks
--   fact_employee                 route anchor geocode + office geo
--
-- ANCHOR: planned_emp_order = 0 is the employee farthest from office
-- (verified: order 0 = 15.1 km avg, order 1 = 13.0, order 2 = 11.4).
-- For LOGIN that is the first pickup; for LOGOUT the last drop. This is
-- the single most discriminating geographic feature for the recommender.
--
-- Geos are returned as raw 'lat,lng' text on purpose — parsing happens
-- downstream so this query stays dialect-portable.
--
-- If it times out, run it one month at a time and concatenate the CSVs.
-- =====================================================================

SELECT
    ft.trip_id,
    ft.bunit_id,
    ft.office,
    ft.office_locid,
    ft.trip_date,
    ft.shift,
    ft.trip_direction,

    -- ---------- timings: needed for the duty-chain / feasibility layer ----------
    ft.planned_start_time,
    ft.planned_end_time,
    ft.actual_start_time,
    ft.actual_end_time,
    ft.vendor_allocation_time,
    ft.cab_allocation_time,

    -- ---------- who did it (the label we are trying to predict) ----------
    ft.actual_cab_registration,
    ft.cab_id,
    ft.actual_vehicle_guid,
    ft.mis_driver_id,
    ft.driver_name,

    -- ---------- vendor / subvendor ----------
    ft.vendor_id,
    ft.realvendorid,
    (
        SELECT max(st.subvendor_name)
        FROM firstcut.subvendor_trips_mapping st
        WHERE st.trip_id = ft.trip_id
          AND st.buid    = ft.bunit_id
    ) AS subvendor_name,

    -- ---------- hard-filter attributes ----------
    ft.cabtype,
    ft.actual_cabtype,
    ft.actual_cab_capacity,
    ft.desired_cab_capacity,
    ft.actual_cab_fuel_type,
    ft.escort_requirement,
    ft.actual_escort,
    ft.is_cab_virtual,
    ft.adhoc,

    -- ---------- outcome quality (tiebreaker + fallback ranking) ----------
    -- delay_reason is one of NODELAY / DRIVER / EMPLOYEE / TRAFFIC.
    -- Anything other than NODELAY means late, and delay_or_early_minutes
    -- gives the magnitude (negative = early). DRIVER is the cab's own fault
    -- and is the reliability signal: p50 3.3% of trips, p90 10.1% across cabs.
    ft.delay_reason,
    ft.delay_or_early_minutes,
    ft.noshow_cnt,
    ft.is_cab_nc,
    ft.is_driver_nc,
    ft.compliance_violation,

    -- ---------- size / distance ----------
    ft.plannedemployee_cnt,
    ft.actualemployee_cnt,
    ft.planned_km,
    ft.trip_approved_km,

    -- ---------- geography (from fact_employee) ----------
    fe.anchor_pickup_geo,     -- LOGIN  : first pickup  (farthest employee)
    fe.anchor_drop_geo,       -- LOGOUT : last drop     (farthest employee)
    fe.office_geo,
    fe.n_employees,

    ft.trip_state_text,
    ft.trip_status_text

FROM firstcut.fact_trip ft

LEFT JOIN (
    SELECT
        fe.trip_id,
        fe.bunit_id,
        max(CASE WHEN fe.planned_emp_order = 0 THEN fe.planned_pickup_geo END) AS anchor_pickup_geo,
        max(CASE WHEN fe.planned_emp_order = 0 THEN fe.planned_drop_geo   END) AS anchor_drop_geo,
        max(fe.office_geo)                                                     AS office_geo,
        count(DISTINCT fe.employee_id)                                         AS n_employees
    FROM firstcut.fact_employee fe
    WHERE fe.trip_date >= {{start_date}}
      AND fe.trip_date <  {{end_date}}
      AND fe.is_test_user = FALSE
      AND fe.bunit_id IN (
            'archcapital1-ACHYD','arcelormittal-AHyd','blackbaud-Hyd','cibc-chyd',
            'dbs-DHyd','easports-EAHyd','ensono-Ensono','evernorth-HYD',
            'gchariot2-GHyd','goc-GocHyd','hartford-Hyd','hgs-HInd','hitachi-HitHyd',
            'ibm2-IHyd','infinx-IHYD','infosys2-GHyd','ivycomptech-IVYHyd',
            'kyndryl-India','mcdonalds-Hyd','medtronic-MEHyd','minimed-Hyd',
            'modmed-HYD','nielsen-nielsen','nttd-Hyd','oaktree-Hyd','pegasystems-PHyd',
            'r1rcm-RHyd','sonoco-Hyd','trimont-IND','trinet-Hyd','uber-UHyd',
            'ubs1-UHyd','wisepayments-HYD'
      )
    GROUP BY fe.trip_id, fe.bunit_id
) fe
  ON  fe.trip_id  = ft.trip_id
  AND fe.bunit_id = ft.bunit_id

WHERE ft.trip_date >= {{start_date}}
  AND ft.trip_date <  {{end_date}}
  AND ft.trip_state_text  = 'COMPLETED'
  AND ft.trip_status_text = 'ACTIVE'

  -- ---------- Hyderabad only (mirrors your city model) ----------
  AND ft.bunit_id IN (
        'archcapital1-ACHYD','arcelormittal-AHyd','blackbaud-Hyd','cibc-chyd',
        'dbs-DHyd','easports-EAHyd','ensono-Ensono','evernorth-HYD',
        'gchariot2-GHyd','goc-GocHyd','hartford-Hyd','hgs-HInd','hitachi-HitHyd',
        'ibm2-IHyd','infinx-IHYD','infosys2-GHyd','ivycomptech-IVYHyd',
        'kyndryl-India','mcdonalds-Hyd','medtronic-MEHyd','minimed-Hyd',
        'modmed-HYD','nielsen-nielsen','nttd-Hyd','oaktree-Hyd','pegasystems-PHyd',
        'r1rcm-RHyd','sonoco-Hyd','trimont-IND','trinet-Hyd','uber-UHyd',
        'ubs1-UHyd','wisepayments-HYD'
  )
  -- multi-city BUs: keep only their Hyderabad offices
  AND (
        ft.bunit_id NOT IN ('hgs-HInd','nielsen-nielsen','trimont-IND',
                            'ensono-Ensono','ivycomptech-IVYHyd',
                            'kyndryl-India','r1rcm-RHyd')
     OR (ft.bunit_id = 'hgs-HInd'          AND ft.office IN ('SEZ HYD DLF Cybercity Block 1-3F',
                                                             'HYD QCity Block B-7F',
                                                             'HYD QCITY BLOCK A-7F'))
     OR (ft.bunit_id = 'nielsen-nielsen'   AND ft.office = 'Hyderabad')
     OR (ft.bunit_id = 'trimont-IND'       AND ft.office = 'Trimont_Hyd')
     OR (ft.bunit_id = 'ensono-Ensono'     AND ft.office = 'Ensono_Hyderabad')
     OR (ft.bunit_id = 'ivycomptech-IVYHyd' AND coalesce(ft.office,'') <> 'IN-PUNE')
     OR (ft.bunit_id = 'kyndryl-India'     AND ft.office = 'HYD_Divyashree-Orion')
     OR (ft.bunit_id = 'r1rcm-RHyd'        AND ft.vendor_id = 'MoveInSync HYD')
  )
  AND NOT (ft.bunit_id = 'arcelormittal-AHyd' AND ft.vendor_id LIKE '%ECO Mobility%')

LIMIT 1048575
