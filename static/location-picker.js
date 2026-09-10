(() => {
  'use strict';

  const SUPPORTED_LANGUAGES = new Set(['ru', 'en', 'ka']);
  const DEFAULT_LANGUAGE = 'ru';
  const COUNTRY_SELECTOR = '[data-country-select]';
  const CITY_SELECTOR = '[data-city-select]';
  const DISPLAY_SELECTOR = '[data-location-country][data-location-city]';
  const scriptElement = document.currentScript;
  const locationsUrl = (() => {
    const configuredUrl = scriptElement?.dataset.locationsUrl;
    const baseUrl = scriptElement?.src || document.baseURI;
    try {
      return new URL(configuredUrl || 'locations.json', baseUrl).href;
    } catch (_error) {
      return '/static/locations.json';
    }
  })();

  const messages = {
    ru: {
      loading: 'Загрузка…',
      countryPlaceholder: 'Выберите страну',
      countryAny: 'Любая страна',
      cityPlaceholder: 'Сначала выберите страну',
      cityChoice: 'Выберите город',
      cityAny: 'Любой город',
      loadError: 'Не удалось загрузить список городов',
    },
    en: {
      loading: 'Loading…',
      countryPlaceholder: 'Select a country',
      countryAny: 'Any country',
      cityPlaceholder: 'Select a country first',
      cityChoice: 'Select a city',
      cityAny: 'Any city',
      loadError: 'Could not load the city list',
    },
    ka: {
      loading: 'იტვირთება…',
      countryPlaceholder: 'აირჩიეთ ქვეყანა',
      countryAny: 'ნებისმიერი ქვეყანა',
      cityPlaceholder: 'ჯერ აირჩიეთ ქვეყანა',
      cityChoice: 'აირჩიეთ ქალაქი',
      cityAny: 'ნებისმიერი ქალაქი',
      loadError: 'ქალაქების სია ვერ ჩაიტვირთა',
    },
  };

  let locations = null;
  let locationsPromise = null;
  let activeLanguage = readDocumentLanguage();
  const pickerPairs = [];

  function normalizeLanguage(value) {
    const language = String(value || '').trim().toLowerCase().split(/[-_]/, 1)[0];
    return SUPPORTED_LANGUAGES.has(language) ? language : DEFAULT_LANGUAGE;
  }

  function readDocumentLanguage() {
    return normalizeLanguage(document.documentElement.lang);
  }

  function languageFromEvent(event) {
    const detail = event?.detail;
    if (typeof detail === 'string') return normalizeLanguage(detail);
    return normalizeLanguage(detail?.language || detail?.lang || detail?.locale || detail?.code || document.documentElement.lang);
  }

  function localizedName(entity, language = activeLanguage) {
    const names = entity?.names;
    if (!names || typeof names !== 'object') return '';
    return String(names[language] || names.ru || names.en || names.ka || '').trim();
  }

  function isSelect(element) {
    return element?.tagName === 'SELECT';
  }

  function createOption(value, label, { disabled = false, selected = false } = {}) {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = label;
    option.disabled = disabled;
    option.selected = selected;
    return option;
  }

  function setOnlyOption(select, label) {
    const option = createOption('', label, { selected: true });
    select.replaceChildren(option);
  }

  function validNames(names) {
    if (!names || typeof names !== 'object') return null;
    const cleanNames = {};
    for (const language of SUPPORTED_LANGUAGES) {
      if (typeof names[language] === 'string' && names[language].trim()) {
        cleanNames[language] = names[language].trim();
      }
    }
    return cleanNames.ru || cleanNames.en || cleanNames.ka ? cleanNames : null;
  }

  function normalizeLocations(payload) {
    if (!payload || typeof payload !== 'object' || !payload.countries || typeof payload.countries !== 'object') {
      throw new TypeError('Invalid locations data');
    }

    const countries = {};
    for (const [rawCode, rawCountry] of Object.entries(payload.countries)) {
      const code = String(rawCode).trim().toUpperCase();
      const countryNames = validNames(rawCountry?.names);
      if (!/^[A-Z]{2}$/.test(code) || !countryNames || !Array.isArray(rawCountry?.cities)) continue;

      const cities = [];
      const knownIds = new Set();
      for (const rawCity of rawCountry.cities) {
        const id = String(rawCity?.id || '').trim();
        const cityNames = validNames(rawCity?.names);
        const lat = Number(rawCity?.lat);
        const lon = Number(rawCity?.lon);
        if (!id || knownIds.has(id) || !cityNames || !Number.isFinite(lat) || !Number.isFinite(lon)) continue;
        if (lat < -90 || lat > 90 || lon < -180 || lon > 180) continue;
        knownIds.add(id);
        cities.push({
          id,
          names: cityNames,
          lat,
          lon,
          region: typeof rawCity.region === 'string' ? rawCity.region.trim() : '',
        });
      }

      countries[code] = { code, names: countryNames, cities };
    }

    if (!Object.keys(countries).length) throw new TypeError('Locations data contains no countries');
    return { version: Number(payload.version) || 1, countries };
  }

  async function loadLocations() {
    if (locations) return locations;
    if (!locationsPromise) {
      locationsPromise = fetch(locationsUrl, {
        headers: { Accept: 'application/json' },
        credentials: 'same-origin',
        cache: 'force-cache',
      })
        .then((response) => {
          if (!response.ok) throw new Error(`Locations request failed: ${response.status}`);
          return response.json();
        })
        .then(normalizeLocations)
        .then((data) => {
          locations = data;
          return data;
        });
    }
    return locationsPromise;
  }

  function pickerScope(countrySelect) {
    return countrySelect.closest('[data-location-picker]') || countrySelect.form || countrySelect.parentElement || document;
  }

  function cityTarget(countrySelect, index, citySelects) {
    const explicitSelector = countrySelect.dataset.cityTarget || countrySelect.getAttribute('data-country-select')?.trim();
    if (explicitSelector) {
      try {
        const explicitTarget = document.getElementById(explicitSelector) || document.querySelector(explicitSelector);
        if (isSelect(explicitTarget) && explicitTarget.matches(CITY_SELECTOR)) return explicitTarget;
      } catch (_error) {
        // An invalid optional selector falls back to the nearest city selector.
      }
    }
    const nearbyTarget = pickerScope(countrySelect).querySelector?.(CITY_SELECTOR);
    return isSelect(nearbyTarget) ? nearbyTarget : citySelects[index];
  }

  function countryByCode(code) {
    return locations?.countries?.[String(code || '').trim().toUpperCase()] || null;
  }

  function cityByReference(country, reference) {
    const value = String(reference || '').trim();
    if (!country || !value) return null;
    return country.cities.find((city) => (
      city.id === value || Object.values(city.names).some((name) => name === value)
    )) || null;
  }

  function optionValue(city) {
    return city.names.ru || city.names.en || city.names.ka || city.id;
  }

  function cityLabel(city, duplicatedNames) {
    const name = localizedName(city);
    return duplicatedNames.has(name) && city.region ? `${name} — ${city.region}` : name;
  }

  function syncSelectionData(pair, countryCode, cityValue = '', cityId = '') {
    const { countrySelect, citySelect, scope } = pair;
    countrySelect.dataset.selectedCountry = countryCode;
    citySelect.dataset.selectedCountry = countryCode;
    citySelect.dataset.selectedCity = cityValue;
    citySelect.dataset.selectedCityId = cityId;
    if (scope instanceof HTMLElement) {
      scope.dataset.selectedCountry = countryCode;
      scope.dataset.selectedCity = cityValue;
      scope.dataset.selectedCityId = cityId;
    }
  }

  function selectedCountryReference(pair) {
    return pair.countrySelect.dataset.selectedCountry
      || pair.scope?.dataset?.selectedCountry
      || pair.countrySelect.value;
  }

  function selectedCityReference(pair) {
    return pair.citySelect.dataset.selectedCity
      || pair.scope?.dataset?.selectedCity
      || pair.citySelect.value;
  }

  function renderCountrySelect(pair) {
    const { countrySelect } = pair;
    const wantedCode = String(selectedCountryReference(pair) || '').toUpperCase();
    const fragment = document.createDocumentFragment();
    const emptyLabel = isTruthyAttribute(countrySelect, 'data-allow-empty')
      ? messages[activeLanguage].countryAny
      : messages[activeLanguage].countryPlaceholder;
    fragment.append(createOption('', countrySelect.dataset.placeholder || emptyLabel));

    for (const country of Object.values(locations.countries)) {
      fragment.append(createOption(country.code, localizedName(country), { selected: country.code === wantedCode }));
    }
    countrySelect.replaceChildren(fragment);
    if (!countryByCode(wantedCode)) countrySelect.value = '';
  }

  function renderCitySelect(pair, requestedCity = selectedCityReference(pair)) {
    const { countrySelect, citySelect } = pair;
    const country = countryByCode(countrySelect.value);
    if (!country) {
      setOnlyOption(citySelect, citySelect.dataset.countryPlaceholder || messages[activeLanguage].cityPlaceholder);
      citySelect.disabled = true;
      syncSelectionData(pair, '', '', '');
      return;
    }

    const selectedCity = cityByReference(country, requestedCity);
    const collator = new Intl.Collator(activeLanguage, { sensitivity: 'base' });
    const cities = [...country.cities].sort((left, right) => collator.compare(localizedName(left), localizedName(right)));
    const nameCounts = new Map();
    for (const city of cities) {
      const name = localizedName(city);
      nameCounts.set(name, (nameCounts.get(name) || 0) + 1);
    }
    const duplicatedNames = new Set([...nameCounts].filter(([, count]) => count > 1).map(([name]) => name));
    const fragment = document.createDocumentFragment();
    const emptyLabel = isTruthyAttribute(citySelect, 'data-allow-empty')
      ? messages[activeLanguage].cityAny
      : messages[activeLanguage].cityChoice;
    fragment.append(createOption('', citySelect.dataset.placeholder || emptyLabel));
    for (const city of cities) {
      const option = createOption(optionValue(city), cityLabel(city, duplicatedNames), {
        selected: city.id === selectedCity?.id,
      });
      option.dataset.locationId = city.id;
      option.dataset.lat = String(city.lat);
      option.dataset.lon = String(city.lon);
      fragment.append(option);
    }
    citySelect.replaceChildren(fragment);
    citySelect.disabled = false;
    syncSelectionData(pair, country.code, selectedCity ? optionValue(selectedCity) : '', selectedCity?.id || '');
  }

  function initializePair(pair) {
    const requestedCountry = selectedCountryReference(pair);
    const requestedCity = selectedCityReference(pair);
    renderCountrySelect(pair);
    if (countryByCode(requestedCountry)) pair.countrySelect.value = String(requestedCountry).toUpperCase();
    renderCitySelect(pair, requestedCity);

    pair.countrySelect.addEventListener('change', () => {
      renderCitySelect(pair, '');
      renderLocationDisplays();
    });
    pair.citySelect.addEventListener('change', () => {
      const selectedOption = pair.citySelect.selectedOptions[0];
      const cityId = selectedOption?.dataset.locationId || '';
      syncSelectionData(pair, pair.countrySelect.value, selectedOption?.value || '', cityId);
      renderLocationDisplays();
    });
  }

  function isTruthyAttribute(element, name) {
    if (!element.hasAttribute(name)) return false;
    const value = element.getAttribute(name);
    return value === '' || !['0', 'false', 'no', 'off'].includes(String(value).toLowerCase());
  }

  function renderLocationDisplay(element) {
    const countryCode = element.dataset.locationCountry?.trim().toUpperCase();
    const country = countryByCode(countryCode);
    const cityReference = element.dataset.locationCity;
    const city = cityByReference(country, cityReference);
    const cityName = city ? localizedName(city) : String(cityReference || '').trim();
    const countryName = country ? localizedName(country) : countryCode;
    const hideRussianCountry = countryCode === 'RU' && isTruthyAttribute(element, 'data-hide-russia-country');
    const parts = hideRussianCountry ? [cityName] : [cityName, countryName];
    element.textContent = parts.filter(Boolean).join(element.dataset.locationSeparator || ', ');
  }

  function renderLocationDisplays() {
    document.querySelectorAll(DISPLAY_SELECTOR).forEach(renderLocationDisplay);
  }

  function rerenderForLanguage() {
    if (!locations) return;
    for (const pair of pickerPairs) {
      const country = selectedCountryReference(pair);
      const city = selectedCityReference(pair);
      renderCountrySelect(pair);
      if (countryByCode(country)) pair.countrySelect.value = String(country).toUpperCase();
      renderCitySelect(pair, city);
    }
    renderLocationDisplays();
  }

  function showLoadingState(countrySelects, citySelects) {
    countrySelects.forEach((select) => {
      setOnlyOption(select, messages[activeLanguage].loading);
      select.disabled = true;
    });
    citySelects.forEach((select) => {
      setOnlyOption(select, messages[activeLanguage].loading);
      select.disabled = true;
    });
  }

  function showLoadError(countrySelects, citySelects) {
    [...countrySelects, ...citySelects].forEach((select) => {
      setOnlyOption(select, messages[activeLanguage].loadError);
      select.disabled = true;
    });
  }

  async function initialize() {
    const countrySelects = [...document.querySelectorAll(COUNTRY_SELECTOR)].filter(isSelect);
    const citySelects = [...document.querySelectorAll(CITY_SELECTOR)].filter(isSelect);
    countrySelects.forEach((countrySelect, index) => {
      const citySelect = cityTarget(countrySelect, index, citySelects);
      if (!citySelect) return;
      if (!countrySelect.dataset.selectedCountry && countrySelect.value) {
        countrySelect.dataset.selectedCountry = countrySelect.value;
      }
      if (!citySelect.dataset.selectedCity && citySelect.value) {
        citySelect.dataset.selectedCity = citySelect.value;
      }
    });
    showLoadingState(countrySelects, citySelects);

    try {
      await loadLocations();
      countrySelects.forEach((countrySelect, index) => {
        const citySelect = cityTarget(countrySelect, index, citySelects);
        if (!citySelect || pickerPairs.some((pair) => pair.citySelect === citySelect)) return;
        const pair = { countrySelect, citySelect, scope: pickerScope(countrySelect) };
        pickerPairs.push(pair);
        countrySelect.disabled = false;
        initializePair(pair);
      });
      renderLocationDisplays();
      document.dispatchEvent(new CustomEvent('zooland:locationsready', {
        detail: { countries: Object.keys(locations.countries) },
      }));
    } catch (error) {
      showLoadError(countrySelects, citySelects);
      console.error('ZooLand location picker:', error);
    }
  }

  function handleLanguageChange(event) {
    activeLanguage = languageFromEvent(event);
    rerenderForLanguage();
  }

  document.addEventListener('zooland:languagechange', handleLanguageChange);
  window.addEventListener('zooland:languagechange', handleLanguageChange);

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initialize, { once: true });
  } else {
    initialize();
  }
})();
