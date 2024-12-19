Attribute VB_Name = "Module1"
Sub CreateCombinationChart()
    Dim ws As Worksheet
    Dim chartSheet As Worksheet
    Dim chartObj As ChartObject
    Dim myChart As Chart
    Dim seriesCount As Integer
    Dim i As Integer
    Dim dataRange As Range
    Dim isoWeeksRange As Range
    Dim metricColumn As Range
    Dim yearRange As Range
    Dim campaignGroupRange As Range
    Dim filteredRange As Range
    Dim currentYear As Integer
    Dim campaignGroup As String
    Dim seriesXValues As Range
    Dim seriesValues As Range
    Dim row As Range ' New variable to represent each row in filteredRange
    
    ' Set your worksheet containing the data
    Set ws = ThisWorkbook.Sheets("--DATA--WTD--")
    
    ' Define the range for ISO Weeks (X-axis values)
    Set isoWeeksRange = ws.Range(ws.Cells(1, 5), ws.Cells(1, 56)) ' Columns E to BD, Row 1
    Debug.Print "ISO Weeks Range: " & isoWeeksRange.Address
    
    ' Define the range for the metric "FF_Purchase_Event_Count"
    Set metricColumn = ws.Range("A:A") ' Assuming metric is in column A
    Set yearRange = ws.Range("B:B") ' Assuming Year is in column B
    Set campaignGroupRange = ws.Range("D:D") ' Assuming Campaign_Group is in column D

    ' Filter to only include rows for "FF_Purchase_Event_Count"
    Set filteredRange = Nothing
    For i = 3 To ws.Cells(ws.Rows.Count, "A").End(xlUp).row ' Start from row 3 to the last row with data
        If ws.Cells(i, 1).Value = "FF_Purchase_Event_Count" And _
           ((ws.Cells(i, 2).Value = 2024 And ws.Cells(i, 4).Value = "Paid Total") Or _
            (ws.Cells(i, 2).Value = 2024 And ws.Cells(i, 4).Value = "Non-Paid Total") Or _
            (ws.Cells(i, 2).Value = 2023 And ws.Cells(i, 4).Value = "Grand Total")) Then
            ' Add the row to the filtered range
            If filteredRange Is Nothing Then
                Set filteredRange = ws.Rows(i)
            Else
                ' Correctly accumulate the ranges using Union
                Set filteredRange = Union(filteredRange, ws.Rows(i))
            End If
        End If
    Next i
    
    ' Debugging: Print filtered range address after loop
    If Not filteredRange Is Nothing Then
        Debug.Print "Filtered Range: " & filteredRange.Address
    Else
        Debug.Print "No rows matched the criteria."
    End If

    ' If there is no data to chart, exit the sub
    If filteredRange Is Nothing Then
        MsgBox "No data available for the specified filters.", vbExclamation
        Exit Sub
    End If
    
    ' Create a new worksheet for the chart if it doesn't exist
    On Error Resume Next
    Set chartSheet = ThisWorkbook.Sheets("WTD_CHART")
    On Error GoTo 0
    If chartSheet Is Nothing Then
        Set chartSheet = ThisWorkbook.Sheets.Add(After:=ThisWorkbook.Sheets(ThisWorkbook.Sheets.Count))
        chartSheet.Name = "WTD_CHART"
    End If
    
    ' Clear any existing charts in the chart sheet
    For Each chartObj In chartSheet.ChartObjects
        chartObj.Delete
    Next chartObj
    
    ' Create the chart on the new worksheet
    Set chartObj = chartSheet.ChartObjects.Add(Left:=50, Width:=800, Top:=20, Height:=400)
    chartObj.Name = "Combination Chart"
    Set myChart = chartObj.Chart
    
    ' Set the chart type to Column Clustered initially
    myChart.ChartType = xlColumnClustered
    
    ' Add each series to the chart from the filtered range
    Dim rowIndex As Integer
    rowIndex = 1 ' Initialize row index for debugging
    
    For Each row In filteredRange.Rows
        ' Define series XValues and Values ranges
        Set seriesXValues = isoWeeksRange
        Set seriesValues = row.Cells(1, 5).Resize(1, 52) ' Columns E to BD for the current row
        
        ' Debugging: Print series ranges
        Debug.Print "Series " & rowIndex & " XValues: " & seriesXValues.Address
        Debug.Print "Series " & rowIndex & " Values: " & seriesValues.Address
        
        ' Check if seriesValues contains valid data before adding
        If WorksheetFunction.Count(seriesValues) > 0 Then
            With myChart.SeriesCollection.NewSeries
                .Name = row.Cells(1, 4).Value & " " & row.Cells(1, 2).Value ' Campaign_Group + Year
                .XValues = seriesXValues
                .Values = seriesValues
                
                ' Determine the chart type for each series
                If row.Cells(1, 2).Value = 2024 Then
                    .ChartType = xlColumnStacked ' Use Stacked Column for 2024 data
                    .AxisGroup = xlPrimary ' Set all 2024 data on the primary axis
                ElseIf row.Cells(1, 2).Value = 2023 Then
                    .ChartType = xlLine ' Use Line for 2023 Grand Total
                    .AxisGroup = xlPrimary ' Use primary axis for consistent comparison
                End If
            End With
        Else
            Debug.Print "Series " & rowIndex & " contains no data and will not be added."
        End If
        rowIndex = rowIndex + 1 ' Increment row index for debugging
    Next row
    
    ' Set chart title and axis titles
    myChart.HasTitle = True
    myChart.ChartTitle.Text = "Combination Chart for FF_Purchase_Event_Count"
    
    With myChart.Axes(xlCategory)
        .HasTitle = True
        .AxisTitle.Text = "ISO Weeks"
    End With
    
    With myChart.Axes(xlValue)
        .HasTitle = True
        .AxisTitle.Text = "Purchase Event Count"
    End With
    
    ' Adjust the primary axis group for all series
    For i = 1 To myChart.SeriesCollection.Count
        myChart.SeriesCollection(i).AxisGroup = xlPrimary
    Next i
    
    ' Optional: Adjust the chart legend and display properties
    myChart.HasLegend = True
    myChart.Legend.Position = xlLegendPositionBottom
    
    ' Optional: Customize the chart appearance as needed
    
    MsgBox "Combination Chart created successfully on 'WTD_CHART' sheet!", vbInformation
End Sub


