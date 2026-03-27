import 'package:flutter/material.dart';
import 'package:geolocator/geolocator.dart';
import 'package:http/http.dart' as http;
import 'dart:convert';

// ── Palette ───────────────────────────────────────────────────────────────
const _kBg = Color(0xFF0C1A35);         // deep navy
const _kCard = Color(0xFF162040);        // slightly lighter navy
const _kAccent = Color(0xFF3B82F6);      // sky blue
const _kWarm = Color(0xFFFBBF24);        // amber sun
const _kText = Colors.white;
const _kSubtext = Color(0xFF93C5FD);     // light blue

// ── Weather code → (Bulgarian label, icon) ─────────────────────────────────
String _weatherLabel(int code) {
  if (code == 0) return 'Ясно небе';
  if (code <= 2) return 'Предимно слънчево';
  if (code == 3) return 'Облачно';
  if (code <= 49) return 'Мъгла';
  if (code <= 59) return 'Ръмеж';
  if (code <= 69) return 'Дъжд';
  if (code <= 79) return 'Снеговалеж';
  if (code <= 82) return 'Валежи';
  if (code <= 86) return 'Снежни бури';
  return 'Гръмотевици';
}

IconData _weatherIcon(int code) {
  if (code == 0) return Icons.wb_sunny_rounded;
  if (code <= 2) return Icons.wb_cloudy_outlined;
  if (code == 3) return Icons.cloud_rounded;
  if (code <= 49) return Icons.foggy;
  if (code <= 69) return Icons.grain;
  if (code <= 79) return Icons.ac_unit_rounded;
  if (code <= 82) return Icons.water_drop_rounded;
  return Icons.flash_on_rounded;
}

Color _weatherColor(int code) {
  if (code == 0) return _kWarm;
  if (code <= 2) return const Color(0xFFFFD54F);
  if (code == 3) return const Color(0xFFB0BEC5);
  if (code <= 49) return const Color(0xFF90A4AE);
  if (code <= 69) return const Color(0xFF64B5F6);
  if (code <= 79) return const Color(0xFFCFD8DC);
  return const Color(0xFF5C6BC0);
}

// ── Elderly-friendly daily tip ─────────────────────────────────────────────
String _dailyTip(int code, double maxTemp) {
  if (maxTemp >= 30) return '🌡 Много горещо! Пийте вода по-често и избягвайте излизане следобед.';
  if (maxTemp <= 2) return '🧥 Студено е! Облечете се топло и внимавайте за лед.';
  if (code >= 60 && code <= 69) return '☔ Носете чадър! Очаква се дъжд.';
  if (code >= 70 && code <= 79) return '❄ Снежно! Внимавайте при ходене.';
  if (code >= 95) return '⛈ Гръмотевична буря. Избягвайте открити места.';
  if (code == 0) return '☀ Прекрасно слънчево време! Подходящо за разходка.';
  return '🌤 Умерено хубаво. Подходящо за кратка разходка навън.';
}

// ── Model ─────────────────────────────────────────────────────────────────
class DayForecast {
  final String date;
  final double maxTemp;
  final double minTemp;
  final int weatherCode;

  DayForecast({
    required this.date,
    required this.maxTemp,
    required this.minTemp,
    required this.weatherCode,
  });
}

// ── Screen ─────────────────────────────────────────────────────────────────
class WeatherScreen extends StatefulWidget {
  final Position userPosition;
  const WeatherScreen({super.key, required this.userPosition});

  @override
  State<WeatherScreen> createState() => _WeatherScreenState();
}

class _WeatherScreenState extends State<WeatherScreen> {
  List<DayForecast> _forecast = [];
  double? _currentTemp;
  bool _loading = true;
  String? _error;

  @override
  void initState() {
    super.initState();
    _fetchWeather();
  }

  Future<void> _fetchWeather() async {
    setState(() { _loading = true; _error = null; });
    try {
      final lat = widget.userPosition.latitude;
      final lng = widget.userPosition.longitude;
      final url = Uri.parse(
        'https://api.open-meteo.com/v1/forecast'
        '?latitude=$lat&longitude=$lng'
        '&daily=weathercode,temperature_2m_max,temperature_2m_min'
        '&current=temperature_2m'
        '&current_weather=true'
        '&timezone=auto'
        '&forecast_days=7',
      );
      final resp = await http.get(url);
      if (resp.statusCode == 200) {
        final data = jsonDecode(resp.body);
        final daily = data['daily'] as Map<String, dynamic>;
        final dates = List<String>.from(daily['time']);
        final maxTemps = List<double>.from(daily['temperature_2m_max'].map((v) => v?.toDouble() ?? 0.0));
        final minTemps = List<double>.from(daily['temperature_2m_min'].map((v) => v?.toDouble() ?? 0.0));
        final codes = List<int>.from(daily['weathercode'].map((v) => (v ?? 0).toInt()));

        double? currentTemp;
        final current = data['current'];
        if (current is Map<String, dynamic> && current['temperature_2m'] != null) {
          currentTemp = (current['temperature_2m'] as num).toDouble();
        }
        if (currentTemp == null) {
          final currentWeather = data['current_weather'];
          if (currentWeather is Map<String, dynamic> && currentWeather['temperature'] != null) {
            currentTemp = (currentWeather['temperature'] as num).toDouble();
          }
        }

        setState(() {
          _forecast = List.generate(dates.length, (i) => DayForecast(
            date: dates[i],
            maxTemp: maxTemps[i],
            minTemp: minTemps[i],
            weatherCode: codes[i],
          ));
          _currentTemp = currentTemp;
          _loading = false;
        });
      } else {
        setState(() { _error = 'Неуспешно зареждане на прогнозата.'; _loading = false; });
      }
    } catch (e) {
      setState(() { _error = 'Няма интернет връзка.'; _loading = false; });
    }
  }

  String _dayName(String date) {
    final d = DateTime.parse(date);
    const bg = ['Пон', 'Вт', 'Ср', 'Чет', 'Пет', 'Съб', 'Нед'];
    final now = DateTime.now();
    final today = DateTime(now.year, now.month, now.day);
    final target = DateTime(d.year, d.month, d.day);
    final daysDiff = target.difference(today).inDays;

    if (daysDiff == 0) return 'Днес';
    if (daysDiff == 1) return 'Утре';
    return bg[d.weekday - 1];
  }

  String _dateLabel(String date) {
    final d = DateTime.parse(date);
    return '${d.day.toString().padLeft(2, '0')}.${d.month.toString().padLeft(2, '0')}';
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: _kBg,
      body: SafeArea(
        child: _loading
            ? const Center(child: CircularProgressIndicator(color: _kAccent))
            : _error != null
                ? _buildError()
                : _buildContent(),
      ),
    );
  }

  Widget _buildError() {
    return Center(
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          const Icon(Icons.cloud_off, size: 72, color: _kSubtext),
          const SizedBox(height: 16),
          Text(_error!, style: const TextStyle(color: _kText, fontSize: 18), textAlign: TextAlign.center),
          const SizedBox(height: 24),
          ElevatedButton.icon(
            onPressed: _fetchWeather,
            icon: const Icon(Icons.refresh),
            label: const Text('Опитай отново'),
            style: ElevatedButton.styleFrom(backgroundColor: _kAccent, foregroundColor: _kText),
          ),
        ],
      ),
    );
  }

  Widget _buildContent() {
    final today = _forecast.isNotEmpty ? _forecast[0] : null;
    final upcoming = _forecast.length > 1
        ? _forecast.skip(1).take(5).toList()
        : <DayForecast>[];

    return CustomScrollView(
      slivers: [
        SliverToBoxAdapter(child: _buildHero(today)),
        SliverToBoxAdapter(
          child: Padding(
            padding: const EdgeInsets.fromLTRB(20, 14, 20, 4),
            child: Text('СЛЕДВАЩИТЕ НЯКОЛКО ДНИ',
                style: TextStyle(color: _kSubtext, fontSize: 12, letterSpacing: 1.4, fontWeight: FontWeight.bold)),
          ),
        ),
        if (upcoming.isNotEmpty)
          SliverPadding(
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
            sliver: SliverList(
              delegate: SliverChildBuilderDelegate(
                (ctx, i) => _buildDayCard(upcoming[i]),
                childCount: upcoming.length,
              ),
            ),
          ),
        if (upcoming.isEmpty)
          const SliverToBoxAdapter(
            child: Padding(
              padding: EdgeInsets.fromLTRB(20, 8, 20, 12),
              child: Text(
                'Няма достатъчно данни за следващите дни.',
                style: TextStyle(color: _kSubtext, fontSize: 14),
              ),
            ),
          ),
        const SliverToBoxAdapter(child: SizedBox(height: 24)),
      ],
    );
  }

  Widget _buildHero(DayForecast? today) {
    if (today == null) return const SizedBox();
    final currentDisplay = _currentTemp ?? today.maxTemp;
    return Container(
      margin: const EdgeInsets.fromLTRB(16, 16, 16, 10),
      padding: const EdgeInsets.all(22),
      decoration: BoxDecoration(
        gradient: LinearGradient(
          colors: [
            _kAccent.withValues(alpha: 0.9),
            const Color(0xFF264E9B),
            _kBg,
          ],
          stops: const [0.0, 0.48, 1.0],
          begin: Alignment.topCenter,
          end: Alignment.bottomRight,
        ),
        borderRadius: BorderRadius.circular(24),
        boxShadow: [
          BoxShadow(
            color: const Color(0xFF020617).withValues(alpha: 0.35),
            blurRadius: 26,
            offset: const Offset(0, 12),
          )
        ],
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          const Text(
            'ДНЕШНА ПРОГНОЗА',
            style: TextStyle(
              color: Color(0xFFBFDBFE),
              fontSize: 11,
              letterSpacing: 1.5,
              fontWeight: FontWeight.w700,
            ),
          ),
          const SizedBox(height: 8),
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              Expanded(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Text(
                    '${_dayName(today.date)} • ${_dateLabel(today.date)}',
                    style: const TextStyle(color: _kSubtext, fontSize: 13),
                  ),
                  const SizedBox(height: 2),
                  Text('Сега ${currentDisplay.round()}°C',
                      style: const TextStyle(color: _kText, fontSize: 60, fontWeight: FontWeight.w800, height: 1)),
                  Text(
                    'Макс: ${today.maxTemp.round()}°C',
                    style: const TextStyle(color: _kSubtext, fontSize: 16, fontWeight: FontWeight.w700),
                  ),
                  Text(
                    'Мин: ${today.minTemp.round()}°C',
                    style: const TextStyle(color: _kSubtext, fontSize: 16, fontWeight: FontWeight.w700),
                  ),
                  Text(_weatherLabel(today.weatherCode),
                      style: const TextStyle(color: _kText, fontSize: 18, fontWeight: FontWeight.w500)),
                ]),
              ),
              Icon(_weatherIcon(today.weatherCode), size: 70, color: _weatherColor(today.weatherCode)),
            ],
          ),
          const SizedBox(height: 16),
          Container(
            padding: const EdgeInsets.all(14),
            decoration: BoxDecoration(
              color: Colors.white.withValues(alpha: 0.08),
              borderRadius: BorderRadius.circular(14),
            ),
            child: Text(
              _dailyTip(today.weatherCode, today.maxTemp),
              style: const TextStyle(color: _kText, fontSize: 15, height: 1.4),
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildDayCard(DayForecast day) {
    return AnimatedContainer(
      duration: const Duration(milliseconds: 300),
      margin: const EdgeInsets.symmetric(vertical: 6),
      decoration: BoxDecoration(
        color: _kCard,
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: Colors.white.withValues(alpha: 0.06), width: 1),
      ),
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 12),
        child: Row(
          children: [
            Container(
              width: 48, height: 48,
              decoration: BoxDecoration(
                color: _weatherColor(day.weatherCode).withValues(alpha: 0.15),
                shape: BoxShape.circle,
              ),
              child: Icon(_weatherIcon(day.weatherCode), color: _weatherColor(day.weatherCode), size: 26),
            ),
            const SizedBox(width: 16),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: [
                  Row(
                    children: [
                      Text(
                        _dayName(day.date),
                        style: const TextStyle(color: _kText, fontWeight: FontWeight.w700, fontSize: 18),
                      ),
                      const SizedBox(width: 8),
                      Text(
                        _dateLabel(day.date),
                        style: const TextStyle(color: _kSubtext, fontSize: 12),
                      ),
                    ],
                  ),
                  const SizedBox(height: 4),
                  Text(_weatherLabel(day.weatherCode),
                      style: const TextStyle(color: _kSubtext, fontSize: 14)),
                ],
              ),
            ),
            Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.end,
              children: [
                Text('${day.maxTemp.round()}°',
                    style: const TextStyle(color: _kText, fontWeight: FontWeight.w800, fontSize: 22)),
                Text('${day.minTemp.round()}°',
                    style: const TextStyle(color: _kSubtext, fontSize: 15)),
              ],
            ),
          ],
        ),
      ),
    );
  }
}
