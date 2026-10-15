-- 範例資料：三個人，對應 SPEC 第 4 節的例子。在空的資料庫跑一次
INSERT INTO contractors (name) VALUES ('Alice'), ('Bob'), ('Carol');

INSERT INTO contracts (contractor_id, salary_type, salary_amount, start_date) VALUES
  (1, 'monthly', 50000, '2026-01-01'),  -- 整月 → 50000
  (2, 'monthly', 50000, '2026-03-10'),  -- 3/10 到職，22 天 → 36667
  (3, 'hourly',  183,   '2026-01-01');  -- 時薪

INSERT INTO timesheets (contract_id, work_date, hours) VALUES
  (3, '2026-03-02', 7.5),
  (3, '2026-03-03', 8);                 -- 183 × 15.5 = 2836.5 → 2837
