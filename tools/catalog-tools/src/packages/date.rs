//! Calendar dates (`YYYY-MM-DD`, UTC) without a date library.

use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::{Result, bail};

/// Today's date in UTC.
pub fn today() -> String {
    let seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |elapsed| elapsed.as_secs());
    let days = i64::try_from(seconds / 86_400).unwrap_or(0);
    let (year, month, day) = civil_from_days(days);
    format!("{year:04}-{month:02}-{day:02}")
}

/// Year, month and day of a day count since 1970-01-01 (H. Hinnant's algorithm).
pub fn civil_from_days(days: i64) -> (i64, i64, i64) {
    let shifted = days + 719_468;
    let era = shifted.div_euclid(146_097);
    let day_of_era = shifted.rem_euclid(146_097);
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let shifted_month = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * shifted_month + 2) / 5 + 1;
    let month = if shifted_month < 10 {
        shifted_month + 3
    } else {
        shifted_month - 9
    };
    let year = year_of_era + era * 400 + i64::from(month <= 2);
    (year, month, day)
}

fn is_leap(year: i64) -> bool {
    (year % 4 == 0 && year % 100 != 0) || year % 400 == 0
}

/// Accept only a real calendar date written `YYYY-MM-DD`.
pub fn validate(date: &str) -> Result<()> {
    let parts: Vec<&str> = date.split('-').collect();
    let well_formed = parts.len() == 3
        && [4, 2, 2].iter().zip(&parts).all(|(width, part)| {
            part.len() == *width && part.bytes().all(|byte| byte.is_ascii_digit())
        });
    if !well_formed {
        bail!("date {date:?} is not YYYY-MM-DD");
    }
    let number = |part: &str| part.parse::<i64>().unwrap_or(0);
    let (year, month, day) = (number(parts[0]), number(parts[1]), number(parts[2]));
    let days_in_month = match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if is_leap(year) => 29,
        2 => 28,
        _ => bail!("date {date:?} has no month {month}"),
    };
    if !(1..=days_in_month).contains(&day) {
        bail!("date {date:?} has no day {day}");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn known_days() {
        assert_eq!(civil_from_days(0), (1970, 1, 1));
        assert_eq!(civil_from_days(20_415), (2025, 11, 23));
        assert_eq!(civil_from_days(19_782), (2024, 2, 29));
        assert_eq!(civil_from_days(-1), (1969, 12, 31));
    }

    #[test]
    fn today_is_valid() {
        validate(&today()).unwrap();
    }

    #[test]
    fn validation() {
        validate("2026-11-23").unwrap();
        validate("2024-02-29").unwrap();
        for bad in [
            "2025-02-29",
            "2026-13-01",
            "2026-11-31",
            "26-11-23",
            "2026-1-23",
            "",
            "abcd-ef-gh",
        ] {
            assert!(validate(bad).is_err(), "{bad} should be rejected");
        }
    }
}
