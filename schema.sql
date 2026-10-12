CREATE TABLE
    contractors (
        contractor_id BIGSERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW ()
    );

CREATE TABLE
    contracts (
        contract_id BIGSERIAL PRIMARY KEY,
        salary_type TEXT NOT NULL CHECK (salary_type IN ('hourly', 'monthly')),
        salary_amount NUMERIC(15, 2) NOT NULL CHECK (salary_amount > 0),
        start_date DATE NOT NULL,
        end_date DATE,
        contractor_id BIGINT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW (),
        CONSTRAINT fk_contracts_contractors FOREIGN KEY (contractor_id) REFERENCES contractors (contractor_id) ON DELETE RESTRICT,
        CONSTRAINT chk_end_date_after_or_equal_start_date CHECK (end_date >= start_date)
    );

CREATE TABLE
    timesheets (
        timesheet_id BIGSERIAL PRIMARY KEY,
        hours NUMERIC(4, 1) NOT NULL,
        contract_id BIGINT NOT NULL,
        work_date DATE NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW (),
        CONSTRAINT chk_hours_range CHECK (
            hours > 0
            AND hours <= 24
        ),
        CONSTRAINT uq_timesheets_contract_id_work_date UNIQUE (contract_id, work_date),
        CONSTRAINT fk_timesheets_contracts FOREIGN KEY (contract_id) REFERENCES contracts (contract_id) ON DELETE RESTRICT
    );

CREATE TABLE
    pay_runs (
        pay_run_id BIGSERIAL PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        period_year INT NOT NULL CHECK (period_year >= 1900),
        period_month INT NOT NULL CHECK (period_month BETWEEN 1 AND 12),
        status TEXT NOT NULL CHECK (
            status IN (
                'pending',
                'running',
                'done',
                'failed',
                'finalized'
            )
        ),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW (),
        CONSTRAINT uq_pay_runs_period_year_month UNIQUE (period_year, period_month)
    );

CREATE TABLE
    payslips (
        payslip_id BIGSERIAL PRIMARY KEY,
        pay_run_id BIGINT NOT NULL,
        contractor_id BIGINT NOT NULL,
        amount NUMERIC(15, 2) NOT NULL,
        needs_review BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW (),
        CONSTRAINT uq_payslips_pay_run_id_contractor_id UNIQUE (pay_run_id, contractor_id),
        CONSTRAINT fk_payslips_contractors FOREIGN KEY (contractor_id) REFERENCES contractors (contractor_id) ON DELETE RESTRICT,
        CONSTRAINT fk_payslips_pay_runs FOREIGN KEY (pay_run_id) REFERENCES pay_runs (pay_run_id) ON DELETE RESTRICT
    );

CREATE TABLE
    payslip_lines (
        payslip_line_id BIGSERIAL PRIMARY KEY,
        salary_amount NUMERIC(15, 2) NOT NULL CHECK (salary_amount > 0),
        salary_type TEXT NOT NULL CHECK (salary_type IN ('hourly', 'monthly')),
        work_hours NUMERIC(4, 1) CHECK (work_hours >= 0),
        paid_days INT CHECK (paid_days > 0),
        days_in_month INT CHECK (
            days_in_month >= 28
            AND days_in_month <= 31
        ),
        amount NUMERIC(15, 2) NOT NULL,
        payslip_id BIGINT NOT NULL,
        contract_id BIGINT NOT NULL,
        start_date DATE NOT NULL,
        end_date DATE NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW (),
        CONSTRAINT fk_payslip_lines_contracts FOREIGN KEY (contract_id) REFERENCES contracts (contract_id) ON DELETE RESTRICT,
        CONSTRAINT fk_payslip_lines_payslips FOREIGN KEY (payslip_id) REFERENCES payslips (payslip_id) ON DELETE CASCADE,
        CONSTRAINT chk_end_date_after_or_equal_start_date CHECK (end_date >= start_date),
        CONSTRAINT chk_salary_rules CHECK (
            (
                salary_type = 'monthly'
                AND paid_days IS NOT NULL
                AND days_in_month IS NOT NULL
            )
            OR (
                salary_type = 'hourly'
                AND work_hours IS NOT NULL
            )
        )
    );